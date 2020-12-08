from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5 import QtGui
from PyQt5.QtWidgets import QWidget, QApplication, QLabel, QVBoxLayout, QSlider, QPushButton
from PyQt5.QtGui import QPixmap
from PyQt5.QtCore import pyqtSignal, pyqtSlot, Qt, QThread

#Import other strongtrack functions from files
import render_functions as render
import xml_functions as rx
import decomp_functions as decomp

#Import python libraries
from pythonosc import udp_client
import numpy as np
import cv2
import dlib
import os
import time
from datetime import datetime

# Get the GUI
from ui import Ui_MainWindow

### Declaring some variables. These probably should be consolidated with the other variables in main_window ###
# Frame as original output. Used for calculating scales.
frame_raw = []

# Frame once upscaled and with added graphics
frame_scaled = []

# Frame after upscale but held for when graphics need refreshing
frame_store = np.array(frame_raw)

loc = (0,0)
play = False
showPoints = False

frame_num = 0
active = np.array([999])
active_points = np.zeros([68])
factor = 1
points = []
pointsmove = []

#To track initial mouse click location
lock = (0,0)

morphs = np.zeros((50))

def coeffsToMorphs(coeffs, browcoeffs, points):
    global window

    #Get mouth coeffs
    mouthcoeffs = coeffs[0]

    base = np.load('data/base.npy')

    # Matched against the key drops so that set poses can be used to convert to morphs. The first is a placeholder due to them being neutral
    cindices = np.array([np.zeros((50)), base[19], base[30] + base[31], base[30], base[31], base[36] + base[37], base[41], base[40], base[14], base[15], base[16], base[17],base[18]])

    #Set as zero to begin with
    morphs = np.zeros((50))

    for i in range(mouthcoeffs.shape[0]):
        poseindex = window.setposes[i]

        # Skip the first because it represents neutral pose or brows (not in morphs). This is probably unncessary due to them being zeros but....whatever
        if poseindex != 0 and poseindex != 8 and poseindex != 9:

            cindex = cindices[poseindex] * mouthcoeffs[i]

            morphs = morphs + cindex

    points2 = np.array(points)

    # seal the lips if points indicate sealed lips
    dist = sum(abs(points2[66] - points2[62]))
    
    # Distance within which to gradually seal lips. 30 at width of 570
    width_points = 570 / (points[16][0] - points[0][0])

    zone = 30 / width_points
    #print(width_points, zone, dist)
    if dist <= zone:
        dist = (zone - dist) / zone

        morphs[20] = morphs[19] * dist

    #Brows
    cindex = cindices[8]*browcoeffs[0][1]
    morphs = morphs + cindex

    cindex = cindices[9]*browcoeffs[1][1]
    morphs = morphs + cindex

    browcentre = (browcoeffs[0][0]+browcoeffs[1][0])/2
    cindex = cindices[10] * browcentre
    morphs = morphs + cindex

    cindex = cindices[11]*browcoeffs[0][0]
    morphs = morphs + cindex

    cindex = cindices[12] * browcoeffs[1][0]
    morphs = morphs + cindex

    return morphs

def getEasyBox(frame):
    padding = int(frame.shape[1]/10.8)
    box = dlib.rectangle(padding,padding,frame.shape[1]-padding, frame.shape[0]-padding)
    return box

def findLandmarks(frame, predictor):
    box = getEasyBox(frame)
    frame = render.drawBox(frame, box)
    
    shape = predictor(frame, box)
    
    points = getPoints(shape)

    return points

def getPoints(shape):

    points = []

    for i in range(68):
        coords = []
        coords.append((shape.part(i).x))
        coords.append((shape.part(i).y))
        points.append(coords)

    return points

def getPointsWebcam(shape):

    points = []

    for i in range(68):
        coords = []
        coords.append((shape.part(i).x))
        coords.append((shape.part(i).y))
        points.append(coords)

    return points

def getPointsWebcamScale(shape, factor):

    points = []

    for i in range(68):
        coords = []
        coords.append((shape.part(i).x)*factor)
        coords.append((shape.part(i).y)*factor)
        points.append(coords)

    return points

def trainModel(xml_path):
    global window
    
    check = rx.convertXMLPoints(xml_path)

    if check.shape[0] == 1:
        window.options.oversampling_amount = 1
    else:
        window.options.oversampling_amount = int((600/check.shape[0])+1)
        
    dlib.train_shape_predictor(xml_path, window.predictor_name, window.options)
    predictor = dlib.shape_predictor(window.predictor_name)

    return predictor

#Handles update of frame for mouse and pause events
def updateFramePoints(points):
    
    #Get scaled frame without graphics
    frame = np.array(frame_store)
    #Add lines
    frame = render.drawFace(frame, points, 'full')
    # Add control points
    frame = render.drawControlPoints(points, frame, active)

    return frame

def reposNeutral(neutral_points, points):

    #Scale neutral points to
    width_points = points[16][0] - points[0][0]
    width_neutral = neutral_points[16][0] - neutral_points[0][0]
    width_fac = width_neutral / width_points

    neutral_points = np.divide(neutral_points, [width_fac, width_fac]).astype(int)

    nose_top = points[27]
    nose_top_neutral = neutral_points[27]
    nose_delta = (nose_top - nose_top_neutral)

    neutral_points = neutral_points + nose_delta

    return neutral_points

def getTimeInfo(fps, length):
    length_int = int(length)
    length_diff = (length - length_int)

    framerate_length = 1000 / fps
    framerate_length_int = int(framerate_length)
    framerate_length_diff = (framerate_length - framerate_length_int)

    total_diff = length_diff + framerate_length_diff
    total_int = framerate_length_int - length_int

    return total_diff, total_int

def scaleDlibBox(box, factor):
    left = box.left()*factor
    top = box.top()*factor
    right = box.right()*factor
    bottom = box.bottom()*factor
    scaledBox = dlib.rectangle(int(left),int(top),int(right),int(bottom))

    return scaledBox

class VideoThread(QThread):
    
    pixmap_signal = pyqtSignal(np.ndarray)
    
    def run(self):
        
        global window
        global play
        global frame_num
        global factor
    
        global points
        
        global frame_raw
        global frame_scaled
        global frame_store

        while True:
            start = time.time()

            #Check for async tasks after HUD update
            if window.shouldTrain == True:
                window.trainModel()
                window.shouldTrain= False

            if play == True:

                ret, frame_raw = window.cap.read()

                if ret:

                    #Record webcam if recording is ongoing
                    if window.record == True:
                        window.writer.write(frame_raw)

                    factor = window.getFactor()

                    if showPoints == True:

                        targetres = (int(frame_raw.shape[1]*factor),int(frame_raw.shape[0]*factor))

                        #insert code here to prevent crash when frame raw is empty (for some reason?)
                        frame_scaled = frame_raw
                        frame_scaled = cv2.resize(frame_scaled, targetres)

                        #Store for quick retrieval without graphics
                        frame_store = np.array(frame_scaled)

                        #Show points from the selected model
                        if window.model == True:

                            # Use either the prerecorded mode...
                            if window.webcamBox == False:
                                points = findLandmarks(frame_scaled, window.predictor)

                            #...or the webcam mode
                            else:
                                points = window.genericFace * factor
                                gray = cv2.cvtColor(frame_raw, cv2.COLOR_BGR2GRAY)

                                if window.trackBox == True:
                                    dets = window.detector(gray, 0)
                                    if len(dets) != 0:
                                        window.box = dets[0]
                                shape = window.predictor(frame_raw, window.box)
                                boxToDraw = scaleDlibBox(window.box, factor)
                                frame_scaled = render.drawBox(frame_scaled, boxToDraw)

                                points = getPointsWebcamScale(shape, factor)

                        else:

                            points = window.genericFace*factor
                            genericfactor = window.getGenericFactor()

                            points = window.genericFace * genericfactor

                        # OSC Streaming
                        if window.stream == True:

                            mouth_coeffs, browscoeffs, _, _ = decomp.findCoeffsAll(points, window.keyposes, window.keydrops)

                            morphs = coeffsToMorphs(mouth_coeffs, browscoeffs, points)

                            window.client.send_message("/filter", morphs)

                        # Webcam recording
                        if window.record == True:
                            print('recording morphs')

                            mouth_coeffs, browscoeffs, _, _ = decomp.findCoeffsAll(points, window.keyposes,
                                                                                       window.keydrops)

                            morphs = coeffsToMorphs(mouth_coeffs, browscoeffs, points)
                            window.record_morph_store.append(morphs)

                        # For debugging the accuracy morph extraction by reconstruction the face points based on coeffs
                        if window.debug == True:
                            print('showing debug')
                            points_debug = np.tensordot(mouth_coeffs, window.keyposes[0:5], axes=1)
                            frame_scaled = render.drawFace(frame_scaled, points_debug[0], 'full')

                        else:
                            try:
                                frame_scaled = render.drawFace(frame_scaled, points, 'full')
                            except:
                                print('points not found')

                        self.pixmap_signal.emit(frame_scaled)
                        
                    else:
                        targetres = (int(frame_raw.shape[1] * factor), int(frame_raw.shape[0] * factor))

                        frame_scaled = frame_raw
                        frame_scaled = cv2.resize(frame_scaled, targetres)
                        frame_store = np.array(frame_scaled)

                        self.pixmap_signal.emit(frame_scaled)

                    #if window.webcam == False:
                    frame_num = frame_num+1

            if window.webcamBox == False:
                #Handles syncronisation of the video footage, taking into account the time taken to complete process
                #Get End time
                end = time.time()
                #Get duration of process in milliseconds
                length = (end-start)*1000

                total_diff, total_int = getTimeInfo(window.fps, length)
                ###SOMETHING IS WRONG HERE. NOT EXACTLY RIGHT. WILL COMMENT OUT FOR NOW ###
                #time.sleep(total_diff/1000)

                k = cv2.waitKey(total_int)
            else:
                k = cv2.waitKey(10)

class MainWindow(QMainWindow, Ui_MainWindow):

    def __init__(self, *args, **kwargs):
        
        super(MainWindow, self).__init__(*args, **kwargs)

        self.displayingVideo = False

        self.options = dlib.shape_predictor_training_options()
        self.options.oversampling_amount = 300
        self.options.nu = 0.05
        self.options.tree_depth = 2
        self.detector = dlib.get_frontal_face_detector()
        self.webcamFrame = 2

        self.genericFace = np.load('data/baseface.npy')
        self.model = False
        self.stream = False
        self.webcamBox = False
        self.webcamLive = False
        #self.pretrained = False
        self.keydrops = np.zeros((10, 68, 2))
        self.fps = 60
        self.box = dlib.rectangle(0,0,10,10)

        self.detector = dlib.get_frontal_face_detector()
        self.guessPredictor = dlib.shape_predictor('data/guesspredictor.dat')

        self.setupUi(self)

        #HUD images
        self.training = cv2.imread('data/training.png')
        self.shouldTrain = False

        self.display_width = self.vidholder.geometry().width()
        self.display_height = self.vidholder.geometry().height()

        #Debug
        self.overlayNeutral = False
        self.debug = False
        self.trackBox = True
        self.record = False
        self.state = ('nothing')
        #Slider
        self.horizontalSlider.valueChanged.connect(self.moveBar)
        
        #Menu Buttons
        self.actionQuick_Video.triggered.connect(self.setVideo)
        self.actionOpen_Video.triggered.connect(self.openNormalVideo)
        self.actionOpen_Webcam_Video_recorded.triggered.connect(self.openWebcamVideo)
        self.actionOpen_Webcam.triggered.connect(self.openWebcam)
        self.actionPrevious_Model.triggered.connect(self.previousModel)
        self.actionNew_Model.triggered.connect(self.newModel)
        self.actionLoad_Model.triggered.connect(self.loadModel)
        self.actionExit.triggered.connect(self.quit)
        self.actionExport.triggered.connect(self.export)
        self.actionStream_OSC.triggered.connect(self.streamOSC)
        
        #Buttons
        self.button_playPause.clicked.connect(self.pause)
        self.button_prevFrame.clicked.connect(self.prevFrame)
        self.button_nextFrame.clicked.connect(self.nextFrame)
        self.button_weld.clicked.connect(self.weld)
        self.button_neutral.clicked.connect(self.neutral)
        self.button_train.clicked.connect(self.initiateTrain)
        self.button_landmarks.clicked.connect(self.setLandmarks)
        self.button_guess.clicked.connect(self.showGuess)
        self.button_mouth.clicked.connect(self.showGuessMouth)
        self.button_setKeypose.clicked.connect(self.setKeypose)
        self.button_record.clicked.connect(self.recordWebcam)
        self.button_lockWebcamBox.clicked.connect(self.lockWebcamBox)

        #Shortcuts
        self.shortcut = QShortcut(QKeySequence("space"), self)
        self.shortcut.activated.connect(self.pause)
        self.shortcut = QShortcut(QKeySequence("w"), self)
        self.shortcut.activated.connect(self.weld)
        self.shortcut = QShortcut(QKeySequence("n"), self)
        self.shortcut.activated.connect(self.neutral)
        self.shortcut = QShortcut(QKeySequence("t"), self)
        self.shortcut.activated.connect(self.initiateTrain)
        self.shortcut = QShortcut(QKeySequence("f"), self)
        self.shortcut.activated.connect(self.setLandmarks)
        self.shortcut = QShortcut(QKeySequence("a"), self)
        self.shortcut.activated.connect(self.prevFrame)
        self.shortcut = QShortcut(QKeySequence("d"), self)
        self.shortcut.activated.connect(self.nextFrame)
        self.shortcut = QShortcut(QKeySequence("p"), self)
        self.shortcut.activated.connect(self.resetLabels)
        self.shortcut = QShortcut(QKeySequence("g"), self)
        self.shortcut.activated.connect(self.showGuess)
        self.shortcut = QShortcut(QKeySequence("m"), self)
        self.shortcut.activated.connect(self.showGuessMouth)
        self.shortcut = QShortcut(QKeySequence("r"), self)
        self.shortcut.activated.connect(self.recordWebcam)

        self.button_record.hide()
        self.button_lockWebcamBox.hide()
        self.resetLabels()
        self.show()

    def updateLabels(self, state):
        global frame_store

        self.state = state

        if state == 'nothing':
            self.label_keyposes.setEnabled(True)
            self.label_landmarks.setEnabled(True)
            self.label_videotitle.setEnabled(True)
            self.label_videoflavour.show()
            self.label_landmarksflavour.hide()
            self.label_keyposesflavour.hide()
        elif state == 'video_webcam_feed':
            self.label_videotitle.setEnabled(False)
            self.label_videotitle.setText('Video: Webcam feed')
            self.label_videoflavour.hide()
            self.label_landmarksflavour.show()
        elif state == 'video_recording':
            self.label_videotitle.setEnabled(False)
            self.label_videotitle.setText('Video: Opened')
            self.label_videoflavour.hide()
            self.label_landmarksflavour.show()
        elif state == 'video_webcam_recording':
            self.label_videotitle.setEnabled(False)
            self.label_videotitle.setText('Video: Webcam video')
            self.label_videoflavour.hide()
            self.label_landmarksflavour.show()
        elif state == 'awaiting neutral':
            self.label_landmarksflavour.show()
            self.label_landmarks.setText('Landmarks: Awaiting neutral')
            self.label_landmarksflavour.setText('Find a frame where the subject has a neutral expression, place landmarks and log them with F')
        elif state == 'landmarks added':
            self.label_landmarksflavour.show()
            self.label_landmarks.setText('Landmarks: Not trained')
            self.label_landmarksflavour.setText('Continue adding different expressions and press T to begin the first training')
        elif state == 'landmarks trained':
            self.label_landmarksflavour.show()
            self.label_landmarks.setText('Landmarks: Initial training')
            self.label_landmarksflavour.setText('Log at least 5 (very different) expressions and press T whenever you want to update the training')
        elif state == 'landmarks trained enough':
            self.label_landmarks.setText('Landmarks: Training sufficient')
            self.label_landmarksflavour.setText('Continue logging and training landmarks if points are inaccurate')
            self.label_landmarks.setEnabled(False)
            self.label_keyposesflavour.show()
            self.comboBox.setEnabled(True)
            self.button_setKeypose.setEnabled(True)
        elif state == 'First keypose added':
            self.label_keyposesflavour.show()
            self.label_keyposesflavour.setText('Add at least 5 keyposes to get a responsive animation export. Not all keyposes need setting')
        elif state == 'Keyposes added enough':
            self.label_exportready.setEnabled(False)
            self.label_keyposes.setEnabled(False)
            self.label_keyposesflavour.show()
            self.label_landmarks.setEnabled(False)
            self.label_landmarksflavour.show()
            self.label_landmarks.setText('Landmarks: Training sufficient')
            self.label_landmarksflavour.setText('Continue logging and training landmarks if points are inaccurate')
            self.label_exportready.setText('Ready for export or stream')
            self.label_keyposes.setText('Keyposes: Set')
            self.label_keyposesflavour.setText('Sufficient keyposes added for a responsive animation export.')

            self.actionExport.setEnabled(True)
            self.actionStream_OSC.setEnabled(True)

        else:
            print('error: label update not recognised')


    def resetLabels(self):
        self.label_videoflavour.show()
        self.label_landmarksflavour.hide()
        self.label_keyposesflavour.hide()
        self.label_keyposes.setEnabled(True)
        self.label_landmarks.setEnabled(True)
        self.label_exportready.setEnabled(True)
        self.label_videotitle.setEnabled(True)
        self.label_exportready.setText('Not ready for export or stream')
        self.label_videoflavour.setText('Open the file menu and open a video or webcam feed')
        self.label_landmarksflavour.setText('Open the file menu and either create a new model or load a previously created one.')
        self.label_keyposesflavour.setText('Look at the extraction poses drop-down menu and have the subject in the video or feed pull these poses. Pause the video/feed and set the keypose to the matching expression')
        self.actionExport.setEnabled(False)
        self.actionStream_OSC.setEnabled(False)
        self.comboBox.setEnabled(False)
        self.button_setKeypose.setEnabled(False)
        self.button_train.setEnabled(False)
        self.button_neutral.setEnabled(False)
        self.button_lockWebcamBox.setEnabled(True)

    def resetLandmarkKeyposeLabels(self):

        self.label_landmarksflavour.hide()
        self.label_keyposesflavour.hide()
        self.label_keyposes.setEnabled(True)
        self.label_landmarks.setEnabled(True)
        self.label_exportready.setEnabled(True)
        self.label_exportready.setText('Not ready for export or stream')
        self.label_landmarksflavour.setText('Open the file menu and either create a new model or load a previously created one.')
        self.label_keyposesflavour.setText('Look at the extraction poses drop-down menu and have the subject in the video or feed pull these poses. Pause the video/feed and set the keypose to the matching expression')
        self.actionExport.setEnabled(False)
        self.actionStream_OSC.setEnabled(False)
        self.comboBox.setEnabled(False)
        self.button_setKeypose.setEnabled(False)
        self.button_train.setEnabled(False)
        self.button_neutral.setEnabled(False)

    def lockWebcamBox(self):
        if self.trackBox == True:
            self.trackBox = False
            self.button_lockWebcamBox.setText('Restart box tracking')
        else:
            self.trackBox = True
            self.button_lockWebcamBox.setText('Lock Box position ')

    def showGuess(self):

        global frame_scaled
        global points

        if play == False:

            dets = self.detector(frame_store, 0)

            if len(dets) != 0:
                box = dets[0]
                frame_scaled = render.drawBox(frame_scaled, box)
                shape = self.guessPredictor(frame_scaled, box)
                points = getPointsWebcam(shape)
                frame_scaled = updateFramePoints(points)
                self.update_image_paused(frame_scaled)
            else:
                message = QMessageBox.about(self, 'Show Guess','Face not detected in video')

    def showGuessMouth(self):

        global frame_scaled
        global points

        print('guess mouth')

        if play == False:

            dets = self.detector(frame_store, 0)

            if len(dets) != 0:
                box = dets[0]
                frame_scaled = render.drawBox(frame_scaled, box)
                shape = self.guessPredictor(frame_scaled, box)
                points_mouth = getPointsWebcam(shape)
                points[48:68] = points_mouth[48:68]
                frame_scaled = updateFramePoints(points)
                self.update_image_paused(frame_scaled)
            else:
                message = QMessageBox.about(self, 'Show Guess','Face not detected in video')

    def recordWebcam(self):

        if self.record == True:
            self.button_record.setText('Record webcam video and animation')
            self.record = False
            self.writer.release()
            morphs_store = np.array(self.record_morph_store)
            fileName = self.record_name + '_morphs.txt'
            np.savetxt(fileName, morphs_store)

            print('export successful')
            self.button_lockWebcamBox.setEnabled(False)
            self.trackBox = True
            self.button_lockWebcamBox.setText('Lock Box position ')

        else:
            textFramerate, okPressedFramerate = QInputDialog.getInt(self, "Enter Framerate","Framerate", QLineEdit.Normal, 30)
            if okPressedFramerate == True:
                self.button_record.setText('Stop recording')
                now = datetime.now()
                dt_string = now.strftime("_%d%m%Y_%H%M%S")
                self.record_name = 'projects/video/webcam_record' + dt_string
                framerate = textFramerate
                resolution = (frame_raw.shape[1], frame_raw.shape[0])
                self.writer = cv2.VideoWriter(self.record_name + '.avi', cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'), framerate,resolution)
                self.prepKeyposes()

                self.button_lockWebcamBox.setEnabled(True)

                self.record = True
                self.record_morph_store =[]

    def debugNeutral(self):
        self.overlayNeutral = True
        self.neutral_points = rx.convertXMLPoints(self.xml_path)[0]

    def debugSolution(self):
        self.debug = True

    def debugPoints(self):
        global showPoints
        global factor
        
        #showPoints = True
        factor = self.label.width()/frame_raw.shape[1]

        self.checkPredictor()
        
    def moveBar(self):
        
        global play
        global window
        global frame_num
        global points
        global frame_raw
        global frame_store
        global frame_scaled

        if play == True:
            play = False
        
        val = self.horizontalSlider.value()
        
        frame_num = val
        
        self.cap.set(cv2.CAP_PROP_POS_FRAMES,val)
            
        ret, frame_raw = window.cap.read()
        
        if ret:
            targetres = (int(frame_raw.shape[1]*factor),int(frame_raw.shape[0]*factor))

            frame_scaled = frame_raw
            frame_scaled = cv2.resize(frame_scaled, targetres)
            frame_store = np.array(frame_scaled)


            if showPoints == True:

                if self.model == True:
                    if self.webcamBox ==True:

                        if window.trackBox == True:
                            gray = cv2.cvtColor(frame_raw, cv2.COLOR_BGR2GRAY)
                            dets = window.detector(gray, 0)
                            if len(dets) != 0:
                                window.box = dets[0]

                        shape = window.predictor(frame_raw, window.box)
                        boxToDraw = scaleDlibBox(window.box, factor)
                        frame_scaled = updateFramePoints(points)
                        frame_scaled = render.drawBox(frame_scaled, boxToDraw)

                        points = getPointsWebcamScale(shape, factor)

                    else:
                        points = findLandmarks(frame_scaled, window.predictor)

                        frame_scaled = updateFramePoints(points)

            self.update_image_paused(frame_scaled)

            if self.stream == True:
                coeffs, browcoeffs, _, _ = decomp.findCoeffsAll(points, window.keyposes, self.keydrops)

                morphs = coeffsToMorphs(coeffs, browcoeffs, points)
                self.client.send_message("/filter", morphs)

    def prevFrame(self):
        global frame_num
        global frame_raw
        global frame_store
        global frame_scaled
        global points

        if play == False and frame_num != 0:

            frame_num = frame_num - 1

            self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)

            ret, frame_raw = window.cap.read()

            if ret:

                self.horizontalSlider.setValue(frame_num)

                targetres = (int(frame_raw.shape[1] * factor), int(frame_raw.shape[0] * factor))
                frame_scaled = frame_raw
                frame_scaled = cv2.resize(frame_scaled, targetres)
                frame_store = np.array(frame_scaled)

                if showPoints == True:
                    points = findLandmarks(frame_scaled, window.predictor)

                    frame_scaled = updateFramePoints(points)

                self.update_image_paused(frame_scaled)

    def nextFrame(self):
        global frame_num
        global frame_raw
        global frame_store
        global frame_scaled
        global points

        if play == False:

            frame_num = frame_num + 1

            self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)

            ret, frame_raw = window.cap.read()

            if ret:

                self.horizontalSlider.setValue(frame_num)

                targetres = (int(frame_raw.shape[1] * factor), int(frame_raw.shape[0] * factor))
                frame_scaled = frame_raw
                frame_scaled = cv2.resize(frame_scaled, targetres)
                frame_store = np.array(frame_scaled)

                if showPoints == True:
                    points = findLandmarks(frame_scaled, window.predictor)

                    frame_scaled = updateFramePoints(points)

                self.update_image_paused(frame_scaled)

    def weld(self):
        global points
        if showPoints == True:

            if play ==False:
                print('welding lips')
                points = render.weldLips(points)
                frame_scaled = updateFramePoints(points)
                self.update_image_paused(frame_scaled)

    def neutral(self):
        global points
        if showPoints == True:
            if play == False:
                print('setting lips to neutral')
                points = rx.setNeutral(window.xml_path, points)
                frame_scaled = updateFramePoints(points)
                self.update_image_paused(frame_scaled)

    def trainModel(self):
        global showPoints
        global frame_scaled
        print('starting train')

        self.predictor = trainModel(self.xml_path)

        if self.model == False:
            self.model = True

        if self.webcamBox == True:
            showPoints = True

        check = rx.convertXMLPoints(self.xml_path)

        if self.state == 'landmarks added':
            self.updateLabels('landmarks trained')
            self.update_image_paused(frame_store)

        if check.shape[0] >= 5 and self.state == 'landmarks trained':
            self.updateLabels('landmarks trained enough')
            self.update_image_paused(frame_store)

        self.update_image_paused(frame_store)

    def initiateTrain(self):
        global showPoints

        if showPoints == True or self.webcamBox == True:

            frame_scaled = frame_store.copy()
            x_offset = int((frame_scaled.shape[1] / 2) - (self.training.shape[1] / 2))
            y_offset = int((frame_scaled.shape[0] / 2) - (self.training.shape[0] / 2))
            frame_scaled[y_offset:y_offset + self.training.shape[0],
            x_offset:x_offset + self.training.shape[1]] = self.training
            self.update_image_paused(frame_scaled)

            self.shouldTrain = True

    def setLandmarks(self):

        if showPoints ==True:
            if play ==False:
                if self.webcamBox == False:
                    box = getEasyBox(frame_scaled)
                    filename = os.path.splitext(os.path.split(window.video_path)[1])[0]
                    filepath = 'images/{}_frame{}.jpg'.format(filename,frame_num)
                    cv2.imwrite('projects/' + filepath, frame_store)
                    rx.appendXML(points, box, filepath, window.xml_path)
                    print('Landmark added to model')

                else:
                    dets = self.detector(frame_store, 0)
                    box = dets[0]

                    folder = 'projects/images/webcam/'
                    webcamFrame = len(os.listdir(folder))
                    filepath = 'images/webcam/{}_frame{}.jpg'.format('webcam', str(webcamFrame))
                    rx.appendXML(points, box, filepath, window.xml_path)
                    cv2.imwrite('projects/' + filepath, frame_store)
                    print('Landmark added to model')

                check = rx.convertXMLPoints(self.xml_path)

                if check.shape[0] > 0 and self.state == 'awaiting neutral':
                    self.updateLabels('landmarks added')
                    self.button_train.setEnabled(True)

    def setKeypose(self):
        self.keydrops[self.comboBox.currentIndex()] = points
        print('Added keypose: ' + self.comboBox.currentText())
        export_filename = 'projects/' + self.project_name + '_keyposes.npy'
        np.save(export_filename, self.keydrops)
        self.prepKeyposes()

        if self.keyposes.shape[0] > 0 and self.state == 'landmarks trained enough':
            self.updateLabels('First keypose added')

        if self.keyposes.shape[0] > 4 and self.state == 'First keypose added':
            self.updateLabels('Keyposes added enough')

    def newModel(self):
        global showPoints
        global factor
        global play

        # Ensure video is paused if one is already loaded
        play = False

        text, okPressed = QInputDialog.getText(self, "Create New Model","Enter a project name (no space):", QLineEdit.Normal, "")

        if okPressed and text != '':
            
            xml_path_check = 'projects/' + text + '_source.xml'
            # Check if file already exists
            check = os.path.exists(xml_path_check)
            
            if check == False:
                
                self.project_name = text
                self.xml_path = xml_path_check
                self.checkPredictor()
                
                factor = self.getFactor()
                               
            else:
                buttonReply = QMessageBox.about( self, 'New Model', "Model with file name already exists")
                
    def loadModel(self):
        global play

        # Ensure video is paused
        play = False

        fileName, _ = QFileDialog.getOpenFileName(self,"Load model XML", "","XML Files (*.xml);")

        # Checks if it is associated with strongtrack
        if fileName:
            check = rx.verifyXML(fileName)
            if check == True:
                self.xml_path = fileName

                self.project_name = os.path.splitext(os.path.split(fileName)[1])[0]
                self.project_name = self.project_name[:-7]

                self.checkPredictor()
            else:
                print('This XML file is not associated with strong track.')

    def previousModel(self):

        if self.webcamBox == False:
            filepath = "data/previousModel.txt"
        else:
            filepath = "data/previousModelWebcam.txt"

        if os.path.exists(filepath):
            f = open(filepath, "r")
            fileName = f.read()

            if os.path.exists(fileName):
                check = rx.verifyXML(fileName)

                if check == True:
                    self.xml_path = fileName

                    #Strip out the project name from the full filename
                    self.project_name = os.path.splitext(os.path.split(fileName)[1])[0]
                    self.project_name = self.project_name[:-7]

                    self.checkPredictor()

                else:
                    print('This XML file is not associated with strong track.')
            else:
                print('Filepath not valid. XML file has probably been moved.')
        else:
            print('Filepath not valid. Strongtrack config file has probably been moved.')

    def checkPredictor(self):
        global showPoints
        global factor
        global play
        global frame_store
        global points

        self.resetLandmarkKeyposeLabels()

        self.predictor_name = 'projects/' + self.project_name + '_model.dat'

        # Determine the nature of the provided xml file (or generate one if necessary)
        try:
            check = rx.convertXMLPoints(self.xml_path)
            print('XML file loaded successfully')
        except:
            rx.makeXML(self.xml_path)
            print('No XML file found for project name. Making new one')
            check = rx.convertXMLPoints(self.xml_path)

        if check.shape[0] == 0:
            print('no face values found in xml. Setting generic face')
            self.updateLabels('awaiting neutral')
            self.model = False

            showPoints = True

        else:
            print('model with values found')

            self.predictor = dlib.shape_predictor(self.predictor_name)
            self.model = True
            showPoints = True

            #factor = self.label.width() / frame_raw.shape[1]
            try:
                import_filename = 'projects/' + self.project_name + '_keyposes.npy'
                self.keydrops = np.load(import_filename)
                print('Keyposes extraction poses found and loaded')
                self.updateLabels('Keyposes added enough')
                self.updateLabels('landmarks trained enough')

            except:
                print('No keyposes for extraction found')
                check = rx.convertXMLPoints(self.xml_path)
                if check.shape[0] >= 5:
                    self.updateLabels('landmarks trained enough')
                else:
                    self.updateLabels('landmarks trained')

        # Write filepath of model xml to data files
        if self.webcamBox == False:
            f = open('data/previousModel.txt', "w")
        else:
            f = open('data/previousModelWebcam.txt', "w")

        f.write(self.xml_path)

        # Update the UI with name of model
        self.label_currentmodel.setText(self.project_name)

        # Prepare things for the UI
        if play == False:
            self.button_lockWebcamBox.setEnabled(True)
            self.button_landmarks.setEnabled(True)
            self.button_weld.setEnabled(True)
            self.button_guess.setEnabled(True)
            self.button_mouth.setEnabled(True)
            if self.state != 'awaiting neutral':
                self.button_train.setEnabled(True)
                self.button_neutral.setEnabled(True)
            if self.state == 'landmarks trained enough':
                self.comboBox.setEnabled(True)
                self.button_setKeypose.setEnabled(True)

            print('showing points first time')

            factor = self.getFactor()

            targetres = (int(frame_raw.shape[1] * factor), int(frame_raw.shape[0] * factor))

            frame_scaled = frame_raw
            frame_scaled = cv2.resize(frame_scaled, targetres)
            frame_store = np.array(frame_scaled)

            if self.model == True:
                if self.webcamBox == False:
                    points = findLandmarks(frame_scaled, window.predictor)
                else:
                    dets = window.detector(frame_scaled, 0)

                    if len(dets) != 0:
                        box = dets[0]
                        shape = window.predictor(frame_scaled, box)
                        frame_scaled = render.drawBox(frame_scaled, box)
                        points = getPointsWebcam(shape)

            #Show generic unset points
            else:
                points = window.genericFace * factor
                genericfactor = self.getGenericFactor()
                
                points = window.genericFace *genericfactor

            frame_scaled = render.drawFace(frame_scaled, points, 'full')

            frame_scaled = render.drawControlPoints(points, frame_scaled, active)

            self.update_image_paused(frame_scaled)

    def setVideo(self):
        global play

        # create video thread
        self.thread = VideoThread()
        
        # connect to slot
        self.thread.pixmap_signal.connect(self.update_image)

        # start thread
        self.thread.start()

        if self.webcamBox == False:
            self.updateUIVideo()
        else:
            self.updateUIWebcam()

        self.displayingVideo = True

    def updateUIWebcam(self):
        print('updating for webcam')
        self.horizontalSlider.setValue(0)

        # Activate menu options
        self.actionNew_Model.setEnabled(True)
        self.actionLoad_Model.setEnabled(True)

        self.horizontalSlider.setEnabled(False)
        self.button_playPause.setEnabled(True)
        self.button_nextFrame.setEnabled(False)
        self.button_prevFrame.setEnabled(False)

        f = open("data/previousModelWebcam.txt", "r")
        contents = f.read()

        if contents != 'not set':
            self.actionPrevious_Model.setEnabled(True)

        self.pause()

    def updateUIVideo(self):

        self.horizontalSlider.setMaximum(int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)))

        self.horizontalSlider.setValue(0)


        # Activate menu options
        self.actionNew_Model.setEnabled(True)
        self.actionLoad_Model.setEnabled(True)
        self.actionPrevious_Model.setEnabled(True)
        self.horizontalSlider.setEnabled(True)
        self.button_playPause.setEnabled(True)
        self.button_nextFrame.setEnabled(True)
        self.button_prevFrame.setEnabled(True)

        f = open("data/previousModel.txt", "r")
        contents = f.read()

        if contents != 'not set':
            self.actionPrevious_Model.setEnabled(True)

        self.pause()

    def openWebcam(self):
        global factor
        global play
        global showPoints

        # Ensure video is paused if one is already loaded
        if play == True:
            play = False

        buttonReply = QMessageBox.question(self, 'Experimental', "This is an experimental feature. Live streaming of webcam is not currently as stable as using recorded video. There is however the option to record webcam video whilst streaming so as to return to and improve the footage later, which is what I'd recommend until this feature is better optimised in 0.9 or 1.0.",
                                            QMessageBox.Ok | QMessageBox.Cancel)

        if buttonReply == QMessageBox.Ok:
            buttonReply2 = QMessageBox.question( self, 'Open Webcam', "Opening Webcam may take about 10-15 seconds.", QMessageBox.Ok | QMessageBox.Cancel)

            if buttonReply2 == QMessageBox.Ok:
                if self.model == True:
                    self.model = False
                    showPoints = False

                self.cap = cv2.VideoCapture(0)

                self.resetLabels()

                self.webcamBox = True
                self.webcamLive = True

                # If video thread hasn't been started, start it
                if self.displayingVideo == False:
                    self.setVideo()

                play = True
                self.predictor = dlib.shape_predictor('data/guesspredictor.dat')
                self.actionStream_OSC.setEnabled(False)
                self.actionExport.setEnabled(False)
                self.button_prevFrame.hide()
                self.button_nextFrame.hide()
                self.horizontalSlider.hide()
                self.button_record.show()
                self.button_lockWebcamBox.show()

                self.button_record.setEnabled(True)
                self.horizontalSlider.hide()

                self.updateLabels('video_webcam_feed')

    def openWebcamVideo(self):
        global showPoints

        self.openVideo('webcam')

    def openNormalVideo(self):
        global showPoints

        self.openVideo('normal')

    def openVideo(self, config):

        global factor
        global play
        global showPoints

        # Ensure video is paused if one is already loaded
        play = False
        fileName, _ = QFileDialog.getOpenFileName(self, "Open Video", "", "All Files (*);")

        if fileName:
            checktext = rx.verifyVideo(fileName)
            if checktext == True:

                self.video_path = fileName
                self.cap = cv2.VideoCapture(self.video_path)
                self.fps = self.cap.get(cv2.CAP_PROP_FPS)

                # If video thread hasn't been started, start it
                if self.displayingVideo == False:
                    self.setVideo()
                else:
                    self.horizontalSlider.setMaximum(int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)))
                    self.horizontalSlider.setValue(0)

                    ret, frame_raw = self.cap.read()
                    factor = self.getFactor()

                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

                    self.pause()
                    self.button_prevFrame.show()
                    self.button_nextFrame.show()
                    self.horizontalSlider.show()
                    self.button_record.hide()
                    self.button_lockWebcamBox.hide()
                    self.horizontalSlider.show()

                    self.webcamLive = False

                    self.model = False
                    showPoints = False

                    self.resetLabels()

                if config == 'normal':
                    self.updateLabels('video_recording')
                    self.webcamBox = False

                else:

                    self.updateLabels('video_webcam_recording')
                    self.webcamBox = True

            else:
                message = QMessageBox.about(self, 'Open Video', 'File extension not valid. StrongTrack currently supports files with mp4, avi and mov extensions')

    def mousePressEvent(self, QMouseEvent):
        global loc
        global frame_raw
        global factor        
        global frame_scaled
        global lock
        global points
        global pointsmove
        global active_points
        
        self.setMouseTracking(True)
        self.label.setMouseTracking(True)
        
        loc = (QMouseEvent.x(),QMouseEvent.y()-21)
         
        top = (self.label.y())
        left = (self.label.x())
        right = (self.label.width()+self.label.x())
        bottom = (self.label.height()+self.label.y())
            
        if left <= loc[0] <= right and top <= loc[1] <= bottom:

            # Left click moves single
            if QMouseEvent.button() == Qt.LeftButton:

                if active[0]==999:
                    #Get location relative to picture
                    locpic = (loc[0]-left, loc[1]-top)
                    
                    #Points were found after scaling
                    points2 = np.array(points)
                    
                    for i in range(points2.shape[0]):
                        
                        sub = np.subtract(locpic, points2[i][0:2])
                        dist =(sum(abs(sub)))

                        if dist <=10:
                         
                            active[0] = i
                            lock = locpic 

                            pointsmove=points
                            
                            #Get scaled frame without graphics
                            frame_scaled = np.array(frame_store)
                            frame_scaled = render.drawFace(frame_scaled, points, 'full')
                            frame_scaled = render.drawControlPoints(points, frame_scaled, active)

                            #frame_scaled = updateFramePoints(points)
                            self.update_image_paused(frame_scaled)
                else:
             
                    active[0] = 999
                    active_points = np.zeros([68])
                    points = pointsmove
                    frame_scaled = updateFramePoints(points)
                    self.update_image_paused(frame_scaled)
                    
            # Right click moves group        
            else:
                
                if active[0]==999:
                    #Get location relative to picture
                    locpic = (loc[0]-left, loc[1]-top)
                    
                    #Points were found after scaling
                    points2 = np.array(points)

                    for i in range(points2.shape[0]):
                        
                        sub = np.subtract(locpic, points2[i][0:2])
                        dist =(sum(abs(sub)))
                        
                        if dist <=10:
                         
                            active[0] = i
                            render.activatePortion(i, active_points)
                            lock = locpic

                            pointsmove=points
                            
                            #Get scaled frame without graphics
                            frame_scaled = np.array(frame_store)
                            frame_scaled = render.drawFace(frame_scaled, points, 'full')
                            frame_scaled = render.drawControlPoints(points, frame_scaled, active)

                            #frame_scaled = updateFramePoints(points)
                            self.update_image_paused(frame_scaled)

                else:
                     
                    active[0] = 999
                    active_points = np.zeros([68])
                    points = pointsmove
                    frame_scaled = updateFramePoints(points)
                    self.update_image_paused(frame_scaled)

    def mouseMoveEvent(self, event):
        global points
        global pointsmove
        global frame_scaled
        
        top = (self.label.y())
        left = (self.label.x())
        delta = (lock[0]-event.x()+left,lock[1]-event.y()+top+21)
        
        if active[0] != 999:
                    
            if (sum(active_points)) == 0.0:
                pointsmove = render.movePoints(points, delta, active[0])
            else:
                pointsmove = render.movePointsMultiple(points, active_points, delta)
                
            frame_scaled = updateFramePoints(pointsmove)
            self.update_image_paused(frame_scaled)

    def getGenericFactor(self):
        rawwidth = frame_raw.shape[1]
        rawheight = frame_raw.shape[0]

        scaledwidth = rawwidth  * factor
        scaledheight = rawheight * factor

        scaledratio = scaledwidth/ scaledheight
        genericratio = 1080/1200

        if scaledratio >= genericratio:
            # less portrait
            genericfactor = scaledheight / 1200

        else:
            # more landscape
            genericfactor = scaledwidth / 1080

        return genericfactor

    def getFactor(self):
        global frame_raw


        holderwidth = self.vidholder.geometry().width()
        holderheight = self.vidholder.geometry().height()
        rawwidth = frame_raw.shape[1]
        rawheight = frame_raw.shape[0]

        if rawwidth != 0 and rawheight != 0 and holderheight != 0:
            rawratio = rawwidth / rawheight
            holderratio = holderwidth / holderheight

            # >1 means landscape
            if rawratio >= holderratio:
                factor = holderwidth / rawwidth

            if rawratio < holderratio:
                factor = holderheight / rawheight

        else:
            print('Division by zero was about to happen. Fix this.')
            factor = 1

        return factor

    def resizeEvent(self, event):
        global factor
        global frame_raw
        global showPoints
        global points
        global frame_store
        global frame_scaled

        if self.displayingVideo == True:

            factor = self.getFactor()

            #When play is true, scaling is already handled by the frame update
            if play == False:

                targetres = (int(frame_raw.shape[1]*factor),int(frame_raw.shape[0]*factor))

                frame_scaled = frame_raw
                frame_scaled = cv2.resize(frame_scaled, targetres)

                #Store for quick retrieval without graphics
                frame_store = np.array(frame_scaled)

                if self.model == True:
                    points = findLandmarks(frame_scaled, window.predictor)

                    frame_scaled = render.drawFace(frame_scaled, points, 'full')
                    frame_scaled = render.drawControlPoints(points, frame_scaled, active)

                self.update_image_paused(frame_scaled)

    def export(self):
        global play

        play = False

        fileName, _ = QFileDialog.getSaveFileName(self, "Open Video", "", "Text File (*.txt);")

        if fileName:

            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            frame_num = 0
            morphs_store = []

            self.prepKeyposes()

            while (1):
                frame_num = frame_num + 1
                ret, frame = window.cap.read()

                if ret == True:
                    print(frame_num)

                    if self.webcamBox == False:
                        points = findLandmarks(frame, self.predictor)
                    else:
                        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                        dets = window.detector(gray, 0)
                        if len(dets) != 0:
                            self.box = dets[0]

                        shape = window.predictor(frame, self.box)
                        points = getPointsWebcamScale(shape, factor)

                    mouth_coeffs, brow_coeffs, _, _ = decomp.findCoeffsAll(points,self.keyposes, self.keydrops)
                    morphs = coeffsToMorphs(mouth_coeffs, brow_coeffs, points)
                    morphs_store.append(morphs)

                else:
                    break

            morphs_store = np.array(morphs_store)
            np.savetxt(fileName, morphs_store)

            print('export successful')
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    def streamOSC(self):
        global play

        # Ensure video is paused if one is already loaded
        play = False

        textIP, okPressedIP = QInputDialog.getText(self, "Enter IP Address", "IP Address (leave as default for same machine):",
                                               QLineEdit.Normal, "127.0.0.1")

        if okPressedIP == True:
            textPort, okPressedPort = QInputDialog.getInt(self, "Enter Port",
                                                   "Port (leave as default if using example content):",
                                                   QLineEdit.Normal, 5005)
            if okPressedPort == True:

                if self.model == True or self.webcamBox == True:

                    self.prepKeyposes()

                    self.client = udp_client.SimpleUDPClient(textIP, textPort)

                    self.stream = True

                    print('stream')

    def prepKeyposes(self):

        self.setposes = []

        for i in range(self.keydrops.shape[0]):
            if sum(sum((self.keydrops[i]))) != 0.0:
                self.setposes.append(i)

        keyposes = []

        for entry in self.setposes:
            keyposes.append(self.keydrops[entry])

        self.keyposes = np.array(keyposes)

    def pause(self):
        
        global play
        global frame_scaled
        global points

        if play == True:
            if self.record == False:

                play = False
                self.button_prevFrame.setEnabled(True)
                self.button_nextFrame.setEnabled(True)

                if self.model == True or self.state == 'awaiting neutral' or self.state == 'landmarks added':
                    self.button_landmarks.setEnabled(True)
                    self.button_weld.setEnabled(True)
                    self.button_guess.setEnabled(True)
                    self.button_mouth.setEnabled(True)

                    if self.state != 'awaiting neutral':
                        self.button_train.setEnabled(True)
                        self.button_neutral.setEnabled(True)
                    if self.state == 'landmarks trained enough' or self.state == 'First keypose added' or self.state == 'Keyposes added enough':
                        self.comboBox.setEnabled(True)
                        self.button_setKeypose.setEnabled(True)

                if self.webcamBox == True:
                    self.button_landmarks.setEnabled(True)
                    self.button_train.setEnabled(True)

                if showPoints == True:
                    frame_scaled = updateFramePoints(points)
                    self.update_image_paused(frame_scaled)
                        
        else:
            play = True
            print('pausing')
            self.button_prevFrame.setEnabled(False)
            self.button_nextFrame.setEnabled(False)

            if self.model == True or self.state == 'awaiting neutral' or self.state == 'landmarks added':
                self.button_landmarks.setEnabled(False)
                self.button_weld.setEnabled(False)
                self.button_neutral.setEnabled(False)
                self.comboBox.setEnabled(False)
                self.button_setKeypose.setEnabled(False)
                self.button_train.setEnabled(False)
                self.button_guess.setEnabled(False)
                self.button_mouth.setEnabled(False)

            if self.webcamBox == True:
                self.button_landmarks.setEnabled(False)
                self.button_train.setEnabled(False)

    def quit(self):
        self.close()

    def update_image(self, cv_frame):

        qt_frame = self.convert_cv_qt(cv_frame)

        if play==True:
            self.label.setPixmap(qt_frame)
        
        if play == True:
            self.horizontalSlider.blockSignals(True)
            self.horizontalSlider.setValue(frame_num)
            self.horizontalSlider.blockSignals(False)
        else:
            if showPoints==True:
                frame_scaled = updateFramePoints(points)
                self.update_image_paused(frame_scaled)

    def update_image_paused(self, cv_frame):

        qt_frame = self.convert_cv_qt(cv_frame)

        self.label.setPixmap(qt_frame)
        
        if play == True:
            self.horizontalSlider.blockSignals(True)
            self.horizontalSlider.setValue(frame_num)
            self.horizontalSlider.blockSignals(False)
            
    def convert_cv_qt(self, cv_frame):

        rgb_image = cv2.cvtColor(cv_frame, cv2.COLOR_BGR2RGB)
      
        h, w, ch = rgb_image.shape
        bytes_per_line = ch * w
        
        convert_to_Qt_format = QtGui.QImage(rgb_image.data, w, h, bytes_per_line, QtGui.QImage.Format_RGB888)

        self.display_width = self.vidholder.geometry().width()

        self.display_height = self.vidholder.geometry().height()
        
        p = convert_to_Qt_format.scaled(self.display_width, self.display_height, Qt.KeepAspectRatio)

        return QPixmap.fromImage(p)
    
if __name__ == '__main__':
    app = QApplication([])
    app.setApplicationName("StrongTrack")

    window = MainWindow()
    app.exec_()
