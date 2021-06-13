import sys
import json
from typing import Union, Optional

from PySide2.QtWebEngineCore import QWebEngineUrlRequestInterceptor
from PySide2.QtWebEngineWidgets import (QWebEngineDownloadItem,
                                        QWebEngineSettings,
                                        QWebEngineProfile,
                                        QWebEngineView,
                                        QWebEnginePage)
from PySide2.QtCore import (QCoreApplication,
                            QUrl,
                            QRect,
                            Qt,
                            Slot,
                            QTimer,
                            QEventLoop)
from PySide2.QtWidgets import QApplication, QShortcut
from PySide2.QtGui import QImage


_LOG_CAT = '\x1b[33m[driver]\x1b[0m'

class _UrlRequestInterceptor(QWebEngineUrlRequestInterceptor):
    """Wrapper to use a function with QWebEngineProfile::set_url_request_interceptor()"""

    interceptor:Optional[callable] = None

    def __init__(self, log:callable):
        super().__init__()
        self.log = log

    def interceptRequest(self, info):
        """Block url when self.interceptor(url) is True"""
        url = info.requestUrl().url(QUrl.FormattingOptions(QUrl.None_))
        blocked = self.interceptor(url)
        self.log(_LOG_CAT, 'rejected:\x1b[31m' if blocked else 'accepted:\x1b[32m', url, '\x1b[0m')
        if blocked:
            info.block(True)


class _WebPage(QWebEnginePage):
    """Wrapper to control javascript errors and console messages"""

    js_error = None
    js_trace = False
    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        if self.js_trace:
            # TODO check sourceID
            if level == QWebEnginePage.ErrorMessageLevel:
                self.js_error = f'{message} line {lineNumber}'
            print(f'js:{lineNumber}:', message, file=sys.stderr)


class JsException(Exception):
    pass

def _null_fn(*args):
    """A function that does nothing (used when logger is None)"""
    pass

def _strerr_print(*args):
    """print on stderr"""
    print(*args, file=sys.stderr)
    pass

class AppDriver:
    """QApplication + WebDriver"""
    _excep = None

    def __init__(self, headless=True, logger=False):
        QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
        # QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
        self._app = QApplication(sys.argv)
        self.driver = WebDriver(headless, logger)

    def run(self, f:callable) -> int:
        """
        Call f(driver) then use quit()
        """
        timer = QTimer()
        timer.timeout.connect(lambda: self._run(f))
        timer.setSingleShot(True)
        timer.start(0)
        r = self._app.exec_()
        if self._excep:
            raise self._excep
        return r

    def _run(self, f):
        try:
            f(self.driver)
        except Exception as e:
            self._excep = e
        finally:
            self.quit()

    def exec_(self) -> int:
        return self._app.exec_()

    def quit(self):
        self.driver.quit()
        self._app.quit()

    def __enter__(self):
        return self.driver

    def __exit__(self, type, value, traceback):
        self.quit()


class WebDriver:
    _result = None
    _view = None
    _last_js_error = None
    _dev_view = None
    _headless_view = None

    def __init__(self, headless:bool=True, logger:Union[callable,bool,None]=False):
        if logger == True:
            self.log = _strerr_print
        elif logger:
            self.log = logger
        else:
            self.log = _null_fn

        self._headless = headless
        self._event_loop = QEventLoop()

        self._timer = QTimer()
        self._timer.timeout.connect(self._event_loop.exit)
        self._timer.setSingleShot(True)

        self._profile = QWebEngineProfile(qApp)
        self._profile.downloadRequested.connect(self._download_request)

        self._page = _WebPage(self._profile)
        # self._page.loadFinished.connect(self._event_loop.exit)

        self._interceptor = _UrlRequestInterceptor(self.log)

        # use with execute_script for function results
        self._json_decode = json.JSONDecoder().decode

        self._view = QWebEngineView()
        self._view.loadFinished.connect(self._event_result)
        self._view.setPage(self._page)
        self._view.setAttribute(Qt.WA_NoSystemBackground)
        self._view.show()

        if headless:
            # Wide screen so that a maximum of things is visible in width
            self._view.resize(4096, 8192)
            self._page.settings().setAttribute(QWebEngineSettings.ShowScrollBars, False)
            self._view.setAttribute(Qt.WA_Disabled)
            self._view.setAttribute(Qt.WA_DontShowOnScreen)
        else:
            self._view.resize(1024, 750)

            exit_act = QShortcut("Ctrl+Q", self._view)
            exit_act.activated.connect(self.quit)

            devtool_act = QShortcut("F12", self._view)
            devtool_act.activated.connect(self._toggle_devtools)

    def quit(self) -> None:
        if not self._page:
            return

        if self._view:
            self._view.deleteLater()
        if self._dev_view:
            self._dev_view.deleteLater()
        if self._headless_view:
            self._headless_view.deleteLater()
        self._page.deleteLater()
        self._profile.deleteLater()
        self._page = None

    def set_url_request_interceptor(self, interceptor:Optional[callable]) -> None:
        """
        :param interceptor: A function that takes a url and returns True when
            it needs to be blocked. When interceptor is None, it is disabled.
        """
        self.log(_LOG_CAT, 'init interceptor:', interceptor)
        self._interceptor.interceptor = interceptor
        self._profile.setUrlRequestInterceptor(self._interceptor)

    def get(self, url:str) -> None:
        """Open a url"""
        self.log(_LOG_CAT, 'load:', url)
        self._page.setUrl(url)
        self._event_loop.exec_()
        self.log(_LOG_CAT, 'loaded')
        return self._result

    def sleep_ms(self, ms:int) -> None:
        """Wait ms milliseconds"""
        self.log(_LOG_CAT, 'sleep:', ms)
        self._timer.start(ms)
        self._event_loop.exec_()

    def download(self, url:str, filename:str=None, with_progression:bool=False) -> None:
        """Download a url
        :param with_progression: When True, progression is sent to logger
        """
        self.log(_LOG_CAT, 'download:', url, 'to filename', filename)
        self._with_progression = with_progression and self.log != _null_fn
        self._page.download(url, filename)
        self._event_loop.exec_()

    def execute_script(self, script:str, raise_if_js_error:bool=True):
        """Execute a javascript code.

        The javascript code can return a value with return which must be
        compatible with JSON.stringify().

        :param raise_if_js_error: When True, convert javascript error and
            console.error message to JsException. Otherwise return None when
            a javascript error is encountered.
        """
        self.log(_LOG_CAT, 'script:', script)

        # return an empty string when there is an error in the script
        # return '[]' for nil/undefined value (no return value)
        # otherwise return a json whose value is in a list
        script = f'{{ const ___r = (()=>{{ {script} }})();(___r === null || ___r === undefined) ? "[]" : JSON.stringify([___r]); }}'
        self._page.js_error = None
        self._page.js_trace = True
        self._page.runJavaScript(script, 0, self._event_result)
        self._event_loop.exec_()
        self._page.js_trace = False

        self._last_js_error = self._page.js_error
        self.log(_LOG_CAT, 'result:', (self._last_js_error, self._result))

        if not self._result:
            if raise_if_js_error:
                raise JsException(self._last_js_error)
            return None

        if self._result != '[]':
            return self._json_decode(self._result)[0]

        return None

    def get_last_js_error(self) -> str:
        """Get last javascript error."""
        return self._last_js_error or ''

    def grab(self, x:int=0, y:int=0, w:int=-1, h:int=-1,
             frozen_after_ms:int=0, max_iter:int=10) -> QImage:
        """Get a QImage of the page.

        If the image area is larger than the page, it is automatically truncated.

        A negative value for w or h means page width/height.

        :param frozen_after_ms: start a loop which stops when 2 captures
            separated by a time limit are identical or when max_iter is reached.
        :param max_iter: maximum number of iterations used with frozen_after_ms.
        """
        self.log(_LOG_CAT, 'grap:', (x, y, w, h), 'delay:', frozen_after_ms, 'max_iter:', max_iter)

        page_width, page_height, scroll_top, screen_h = self.execute_script(
            f'''
            e = document.documentElement;
            return [e.scrollWidth, e.scrollHeight, e.scrollTop, e.clientHeight];''')
        # shrink w/h compared to page_width/page_height
        w = page_width      if w < 0 else min(w, page_width-x)
        h = page_height - y if h < 0 else min(h, page_height-y)
        w = min(w + min(0, x), page_width)
        h = min(h + min(0, y), page_height)
        x = max(0, x)
        y = max(0, y)

        if x >= page_width or y >= page_height or w <= 0 or h <= 0:
            return QImage()

        must_resize = (y < scroll_top or y + h > scroll_top + screen_h)

        view = self._view

        if must_resize:
            old_size = view.size()
            if not self._headless:
                view = self._headless_view
                if not view:
                    view = QWebEngineView()
                    view.setAttribute(Qt.WA_Disabled)
                    view.setAttribute(Qt.WA_NoSystemBackground)
                    view.setAttribute(Qt.WA_DontShowOnScreen)
                    self._headless_view = view


            # no scroll when page_height == h
            view_width = page_width if page_height == h else self._view.size().width()
            view.resize(view_width, h)

            if not self._headless:
                view.setPage(self._page)
                view.show()

            self.sleep_ms(200)
            if y != scroll_top:
                self.execute_script(f'e.scrollTop = {y}')
                self.sleep_ms(50)

            rect = QRect(x, 0, w, h)
        else:
            rect = QRect(x, y - scroll_top, w, h)

        img1 = view.grab(rect).toImage()
        if frozen_after_ms:
            for i in range(max_iter):
                self.sleep_ms(frozen_after_ms)
                img2 = self._view.grab(rect).toImage()
                if img1 == img2:
                    break
                img1 = img2

        if must_resize:
            if not self._headless:
                self._view.setPage(self._page)
                view.hide()
            else:
                view.resize(old_size)

            self.execute_script(f'document.documentElement.scrollTop = {scroll_top}')

            self.sleep_ms(200)

        return img1

    def take_screenshot(self, filename:str, format:Optional[str]=None,
                        quality:int=-1,
                        x:int=0, y:int=0, w:int=-1, h:int=-1,
                        frozen_after_ms:int=0, max_iter:int=10) -> bool:
        """Screenshot the page.

        See self.grab()
        """
        self.log(_LOG_CAT, 'screenshot:', filename, 'format:', format)
        img = self.grab(x, y, w, h, frozen_after_ms, max_iter)
        return img.save(filename, format, quality)

    def resize(self, width:int=-1, height:int=-1) -> None:
        """Resize view

        A negative value for width or height means contents size width/height.
        """
        self.log(_LOG_CAT, 'resize:', width, height)
        if width <= 0 or height <= 0:
            size = self._page.contentsSize().toSize()
            if width <= 0: width = size.width()
            if height <= 0: height = size.height()
            self.log(_LOG_CAT, 'resize(computed):', width, height)
        self._view.resize(width, height)

    def contents_size(self) -> tuple[int,int]:
        """Get contents size"""
        size = self._page.contentsSize().toSize()
        return (size.width(), size.height())

    def scroll(self, x:int, y:int) -> None:
        """Scroll at position"""
        self.execute_script(f'window.scroll({x},{y})')

    def enable_devtools(self, enable:bool=True) -> None:
        """Enable or disable devtools"""
        if bool(self._dev_view) == enable:
            return

        if enable:
            self._dev_view = QWebEngineView()
            page = self._dev_view.page()
            self._page.setDevToolsPage(page)
            self._dev_view.show()
        else:
            self._dev_view.deleteLater()
            self._dev_view = None
            self._page.setDevToolsPage(None)

    def _toggle_devtools(self):
        self.enable_devtools(not bool(self._dev_view))

    def _event_result(self, result):
        self._result = result
        self._event_loop.exit()

    def _download_request(self, item:QWebEngineDownloadItem):
        # TODO check url origin
        self._download_item = item
        item.finished.connect(self._download_finished)
        if self._with_progression:
            item.downloadProgress.connect(self._download_progress)
        item.accept()

    def _download_finished(self):
        self.log(_LOG_CAT, 'download, done')
        state = self._download_item.state()
        self._result = True
        if state == QWebEngineDownloadItem.DownloadCompleted:
            self._result = False
        else:
            self.log(_LOG_CAT, self._download_item.interruptReasonString())
        self._download_item = None
        self._event_loop.exit()

    def _download_progress(self, bytesReceived, bytesTotal):
        self.log(_LOG_CAT, f'download {bytesReceived}/{bytesTotal}')

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.quit()
