from Queue import Queue
import os
import threading

from Components.ActionMap import HelpableActionMap
from Components.Console import Console
from Components.PluginComponent import PluginDescriptor
from Components.ServiceEventTracker import InfoBarBase
from Components.ServiceEventTracker import ServiceEventTracker
from Screens.HelpMenu import HelpableScreen
from Screens.InfoBarGenerics import InfoBarNotifications, InfoBarSeek, \
    InfoBarAudioSelection, InfoBarShowHide
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Tools import Notifications

from e2utils import InfoBarAspectChange, WebPixmap, MyAudioSelection, \
    StatusScreen
from enigma import eServiceReference, eTimer, ePythonMessagePump, \
    iPlayableService, fbClass, eRCInput
from server import KodiExtRequestHandler, UDSServer
from Tools.BoundFunction import boundFunction

try:
    from Plugins.Extensions.SubsSupport import SubsSupport, SubsSupportStatus
except ImportError:
    class SubsSupport(object):
        def __init__(self, *args, **kwargs):
            pass
    class SubsSupportStatus(object):
        def __init__(self, *args, **kwargs):
            pass

(OP_CODE_EXIT,
OP_CODE_PLAY,
OP_CODE_PLAY_STATUS,
OP_CODE_PLAY_STOP) = range(4)

KODIRUN_SCRIPT = "kodi;kodiext -T"
KODIEXT_SOCKET = "/tmp/kodiext.socket"

def FBLock():
    print"[KodiLauncher] FBLock"
    fbClass.getInstance().lock()

def FBUnlock():
    print "[KodiLauncher] FBUnlock"
    fbClass.getInstance().unlock()

def RCLock():
    print "[KodiLauncher] RCLock"
    eRCInput.getInstance().lock()

def RCUnlock():
    print "[KodiLauncher] RCUnlock"
    eRCInput.getInstance().unlock()

def kodiStopped(data, retval, extraArgs):
    print '[KodiLauncher] kodi stopped: retval = %d' % retval

class KodiVideoPlayer(InfoBarBase, SubsSupportStatus, SubsSupport, InfoBarShowHide, InfoBarSeek, InfoBarAspectChange, InfoBarAudioSelection, InfoBarNotifications, HelpableScreen, Screen):
    skin = """
        <screen title="custom service source" position="0, 500" size="1280,220" zPosition="1" backgroundColor="#55444444" flags="wfNoBorder">
            <widget name="image" position="20,10" size="200,200" alphatest="on" transparent="1"/>
            <widget source="session.CurrentService" render="Label" position="250,20" size="1230,55" zPosition="1"  font="Regular;24" valign="center" halign="left" transparent="1">
              <convert type="ServiceName">Name</convert>
            </widget>
            <widget source="session.CurrentService" render="PositionGauge" position="250,90" size="970,16" zPosition="4" transparent="1">
                <convert type="ServicePosition">Gauge</convert>
            </widget>
            <widget source="session.CurrentService" render="Progress" position="250,90" size="970,16" zPosition="3" transparent="1">
                <convert type="ServicePosition">Position</convert>
            </widget>
            <widget source="session.CurrentService" render="Label" position="250,120" size="120,28" font="Regular;23" halign="left"   transparent="1">
                <convert type="ServicePosition">Position,ShowHours</convert>
            </widget>
            <widget source="session.CurrentService" render="Label" position="1110,120" size="120,28" font="Regular;23" halign="left"   transparent="1">
                <convert type="ServicePosition">Length,ShowHours</convert>
            </widget>
        </screen>"""

    def __init__(self, session, playlistCallback, nextItemCallback, prevItemCallback, infoCallback, menuCallback):
        Screen.__init__(self, session)
        self.skinName = ['KodiVideoPlayer', 'MoviePlayer']
        statusScreen = self.session.instantiateDialog(StatusScreen)
        InfoBarBase.__init__(self, steal_current_service=True)
        SubsSupport.__init__(self, searchSupport=True, embeddedSupport=True)
        SubsSupportStatus.__init__(self)
        InfoBarSeek.__init__(self)
        InfoBarShowHide.__init__(self)
        InfoBarAspectChange.__init__(self)
        InfoBarAudioSelection.__init__(self)
        InfoBarNotifications.__init__(self)
        HelpableScreen.__init__(self)
        self.playlistCallback = playlistCallback
        self.nextItemCallback = nextItemCallback
        self.prevItemCallback = prevItemCallback
        self.infoCallback = infoCallback
        self.menuCallback = menuCallback
        self.statusScreen = statusScreen
        self.defaultImage = None
        self.postAspectChange.append(self.showAspectChanged)
        self.__image = None
        self.__position = None
        self["image"] = WebPixmap(self.defaultImage, caching=False)
        self["directionActions"] = HelpableActionMap(self, "DirectionActions",
        {
            "downUp": (playlistCallback, _("Show playlist")),
            "upUp": (playlistCallback, _("Show playlist")),
         })

        self["okCancelActions"] = HelpableActionMap(self, "OkCancelActions",
        {
            "cancel": self.close
        })

        self["actions"] = HelpableActionMap(self, "KodiPlayerActions",
        {
            "menuPressed": (menuCallback, _("Show playback menu")),
            "infoPressed": (infoCallback, _("Show playback info")),
            "nextPressed": (nextItemCallback, _("Skip to next item in playlist")),
            "prevPressed": (prevItemCallback, _("Skip to previous item in playlist"))
        })

        self.eventTracker = ServiceEventTracker(self,
        {
            iPlayableService.evStart : self.__evStart,
        })
        self.onClose.append(boundFunction(self.session.deleteDialog, self.statusScreen))

    def __evStart(self):
        if self.__image:
            self["image"].load(self.__image)
        else:
            self["image"].load(self.defaultImage)
        if self.__position:
            Notifications.AddNotificationWithCallback(self.__seekToPosition, MessageBox, _("Resuming playback"), timeout=4, type=MessageBox.TYPE_INFO, enable_input=False)

    def __seekToPosition(self, callback=None):
        self.doSeek(self.__position)

    def setImage(self, image):
        self.__image = image

    def setStartPosition(self, positionInSeconds):
        try:
            self.__position = positionInSeconds * 90 * 1000
        except Exception:
            self.__position = None

    def stopService(self):
        self.session.nav.stopService()

    def playService(self, sref):
        self.session.nav.playService(sref)

    def audioSelection(self):
        self.session.openWithCallback(self.audioSelected, MyAudioSelection, infobar=self)

    def showAspectChanged(self):
        self.statusScreen.setStatus(self.getAspectStr(), "#00ff00")

    def doEofInternal(self, playing):
        self.close()


class E2KodiExtRequestHandler(KodiExtRequestHandler):

    def handle_request(self, opcode, status, data):
        self.server.messageOut.put((status, data))
        self.server.messagePump.send(opcode)
        return self.server.messageIn.get()


class E2KodiExtServer(UDSServer):
    def __init__(self, session, stopCB):
        UDSServer.__init__(self, KODIEXT_SOCKET, E2KodiExtRequestHandler)
        self.session = session
        self.stopCB = stopCB
        self.kodiPlayer = None
        self.subtitles = []
        self.messageIn = Queue()
        self.messageOut = Queue()
        self.messagePump = ePythonMessagePump()
        self.messagePump.recv_msg.get().append(self.messageReceived)

    def shutdown(self):
        self.messagePump.stop()
        self.messagePump = None
        UDSServer.shutdown(self)

    def messageReceived(self, opcode):
        status, data = self.messageOut.get()
        if opcode == OP_CODE_EXIT:
            self.handleExitMessage(status, data)
        elif opcode == OP_CODE_PLAY:
            self.handlePlayMessage(status, data)
        elif opcode == OP_CODE_PLAY_STATUS:
            self.handlePlayStatusMessage(status, data)
        elif opcode == OP_CODE_PLAY_STOP:
            self.handlePlayStopMessage(status, data)

    def handleExitMessage(self, status, data):
        self.messageIn.put((True, None))
        self.stopTimer = eTimer()
        self.stopTimer.callback.append(self.stopCB)
        self.stopTimer.start(500, True)

    def handlePlayStatusMessage(self, status, data):
        self.messageIn.put((self.kodiPlayer is not None, None))

    def handlePlayStopMessage(self, status, data):
        FBLock(); RCLock()
        self.messageIn.put((True, None))

    def handlePlayMessage(self, status, data):
        if data is None:
            self.logger.error("handlePlayMessage: no data!")
            self.messageIn.put((False, None))
            return
        FBUnlock(); RCUnlock()

        subtitles = []
        dataSplit = data.strip().split("\n")
        if len(dataSplit) > 1:
            playPath, subtitlesStr = dataSplit[:2]
            subtitles = subtitlesStr.split("|")
        else:
            playPath = dataSplit[0]
        if playPath.startswith('http'):
            playPathSplit = playPath.split("|")
            if len(playPathSplit) > 1:
                playPath = playPathSplit[0] + "#" + playPathSplit[1]
        self.logger.debug("handlePlayMessage: playPath = %s", playPath)
        for idx, subtitlesPath in enumerate(subtitles):
            self.logger.debug("handlePlayMessage: subtitlesPath[%d] = %s", idx, subtitlesPath)

        sref = eServiceReference(4097, 0, playPath)
        sref.setName(playPath)

        noneFnc = lambda:None
        self.kodiPlayer = self.session.openWithCallback(self.kodiPlayerExitCB, KodiVideoPlayer,
            noneFnc, noneFnc, noneFnc, noneFnc, noneFnc)
        if len(subtitles) > 0 and hasattr(self.kodiPlayer, "loadSubs"):
            # TODO allow to play all subtitles
            subtitlesPath = subtitles[0]
            self.kodiPlayer.loadSubs(subtitlesPath)
        self.kodiPlayer.playService(sref)
        self.messageIn.put((True, None))

    def kodiPlayerExitCB(self, callback=None):
        self.session.nav.stopService()
        self.kodiPlayer = None
        self.subtitles = []

class KodiLauncher(Screen):
    skin = """<screen position="fill" size="1280,720" backgroundColor="#00000000" flags="wfNoBorder" title=" "></screen>"""

    def __init__(self, session):
        Screen.__init__(self, session)
        RCLock()
        self.previousService = self.session.nav.getCurrentlyPlayingServiceReference()
        self.session.nav.stopService()
        self.startupTimer = eTimer()
        self.startupTimer.timeout.get().append(self.startup)
        self.startupTimer.start(500, True)
        self.onClose.append(RCUnlock)

    def startup(self):
        FBLock()
        self.startKodiExtServer()
        self.startKodi()

    def startKodiExtServer(self):
        try:
            os.remove(KODIEXT_SOCKET)
        except:
            pass
        self.server = E2KodiExtServer(self.session, self.stop)
        self.serverThread = threading.Thread(target = self.server.serve_forever)
        self.serverThread.start()

    def startKodi(self):
        Console().ePopen(KODIRUN_SCRIPT, kodiStopped)

    def stop(self):
        FBUnlock()
        self.server.shutdown()
        self.serverThread.join()
        if self.previousService:
            self.session.nav.playService(self.previousService)
        try:
            os.system('cat /tmp/video_outpout > /proc/stb/video/videomode')
            os.system ('rm /tmp/video_outpout')
        except:
            pass
        self.close()

def startLauncher(session, **kwargs):
    try:
        os.system('cat /proc/stb/video/videomode > /tmp/video_outpout')
        os.system ('echo "720p50" > /proc/stb/video/videomode')
    except:
        pass
    RCUnlock()
    session.open(KodiLauncher)

def Plugins(**kwargs):
    return [PluginDescriptor("Kodi", PluginDescriptor.WHERE_PLUGINMENU, "Kodi Launcher", fnc=startLauncher)]

