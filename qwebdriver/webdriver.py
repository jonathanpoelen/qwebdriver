import sys
import json
from typing import Any, Union, Optional, Callable
from PySide6.QtWebEngineCore import (QWebEngineUrlRequestInterceptor,
                                     QWebEngineDownloadRequest,
                                     QWebEngineSettings,
                                     QWebEngineProfile,
                                     QWebEnginePage)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import (QCoreApplication,
                            QUrl,
                            QRect,
                            Qt,
                            QTimer,
                            QPoint,
                            QEvent,
                            QEventLoop)
from PySide6.QtWidgets import (QApplication, QWidget)
from PySide6.QtGui import (QImage, QShortcut, QMouseEvent)


_LOG_CAT = '\x1b[33m[driver]\x1b[0m'


class _UrlRequestInterceptor(QWebEngineUrlRequestInterceptor):
    """Wrapper to use a function with QWebEngineProfile::set_url_request_interceptor()"""

    interceptor: Optional[Callable[[str], bool]] = None

    def __init__(self, log: Callable[[str], bool]):
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

    js_error: Optional[str] = None
    js_trace = False

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        if self.js_trace:
            # TODO check sourceID
            if level == QWebEnginePage.ErrorMessageLevel:
                self.js_error = f'{message} line {lineNumber}'
            print(f'js:{lineNumber}:', message, file=sys.stderr)


class JsException(Exception):
    pass


class DownloadException(Exception):
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
    _excep: Optional[Exception] = None

    def __init__(self, headless: bool = True, logger: bool = False):
        QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
        # QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
        self._app = QApplication(sys.argv)
        self.driver = WebDriver(headless, _strerr_print if logger else _null_fn)

    def run(self, f: Callable[['WebDriver'], None]) -> int:
        """
        Call f(driver) then use quit()
        """
        QTimer.singleShot(0, lambda: self._run(f))
        r = self._app.exec()
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

    def exec(self) -> int:
        return self._app.exec()

    def quit(self) -> None:
        self.driver.quit()
        self._app.quit()

    def __enter__(self):
        return self.driver

    def __exit__(self, type, value, traceback):
        self.quit()


def _left_click(widget: QWidget, event: QEvent.Type, pos: QPoint) -> None:
    QCoreApplication.postEvent(widget,
                               QMouseEvent(event, pos,
                                           Qt.LeftButton,
                                           Qt.MouseButton.NoButton,
                                           Qt.NoModifier))


class WebDriver:
    _result: Any = None
    _last_js_error: Optional[str] = None
    _dev_view: Optional[QWebEngineView] = None
    _headless_view: Optional[QWebEngineView] = None
    _progress_timer: Optional[QTimer] = None
    _downloaded_filename: str = ''
    _enable_exit_event_on_load_finished: bool = False

    def __init__(self, headless: bool = True, logger: Union[Callable[..., None], bool, None] = False):
        if logger is True:
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

        # used with execute_script for function results
        self._json_decode = json.JSONDecoder().decode

        self._view = QWebEngineView()
        self._view.loadFinished.connect(self._event_load_finished)
        self._view.setPage(self._page)
        self._view.setAttribute(Qt.WA_NoSystemBackground)
        self._view.show()

        self._closed = False

        if headless:
            # Wide screen so that a maximum of things is visible in width
            self._view.resize(4096, 8192)
            self._page.settings().setAttribute(QWebEngineSettings.ShowScrollBars, False)
            self._view.setAttribute(Qt.WA_ForceDisabled)
            self._view.setAttribute(Qt.WA_DontShowOnScreen)
            self._view.setAttribute(Qt.WA_ForceUpdatesDisabled)
        else:
            self._view.resize(1024, 750)

            exit_act = QShortcut("Ctrl+Q", self._view)
            exit_act.activated.connect(self.quit)

            devtool_act = QShortcut("F12", self._view)
            devtool_act.activated.connect(self._toggle_devtools)

    def wait_quit(self):
        if not self._headless:
            event_loop = QEventLoop()
            self._view.destroyed.connect(event_loop.exit)
            event_loop.exec()

    def quit(self) -> None:
        if self._closed:
            return

        self._closed = True

        if self._view:
            self._view.deleteLater()
        if self._dev_view:
            self._dev_view.deleteLater()
        if self._headless_view:
            self._headless_view.deleteLater()
        self._page.deleteLater()
        self._profile.deleteLater()

    def set_url_request_interceptor(self, interceptor: Optional[Callable[[str], bool]]) -> None:
        """
        :param interceptor: A function that takes a url and returns True when
            it needs to be blocked. When interceptor is None, it is disabled.
        """
        self.log(_LOG_CAT, 'init interceptor:', interceptor)
        self._interceptor.interceptor = interceptor
        self._profile.setUrlRequestInterceptor(self._interceptor)

    def get(self, url: str) -> None:
        """Open a url"""
        self.log(_LOG_CAT, 'load:', url)
        self._enable_exit_event_on_load_finished = True
        self._page.setUrl(url)
        self._event_loop.exec()
        self._enable_exit_event_on_load_finished = False
        self.log(_LOG_CAT, 'loaded')

    def sleep_ms(self, ms: int) -> None:
        """Wait ms milliseconds"""
        self.log(_LOG_CAT, 'sleep:', ms)
        self._timer.start(ms)
        self._event_loop.exec()

    def save_page(self, filename: str, format: int = 2) -> None:
        self._result = None
        self._with_progression = False
        self._downloaded_filename = filename
        self._downloaded_format = (
            QWebEngineDownloadRequest.SingleHtmlSaveFormat if format == 1 else
            QWebEngineDownloadRequest.CompleteHtmlSaveFormat if format == 2 else
            QWebEngineDownloadRequest.MimeHtmlSaveFormat
        )
        self._page.triggerAction(QWebEnginePage.SavePage)
        self._event_loop.exec()
        if self._result:
            raise self._result

    def download(self, url: str, filename: str = '', with_progression: bool = False) -> None:
        """Download a url
        :param with_progression: When True, progression is sent to logger
        """
        self.log(_LOG_CAT, 'download:', url, 'to filename', filename)
        self._with_progression = with_progression and self.log != _null_fn
        self._result = None
        self._page.download(url, filename)
        self._event_loop.exec()
        if self._result:
            raise self._result

    def execute_script(self, script: str, raise_if_js_error: bool = True):
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
        self._event_loop.exec()
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

    def grab(self, x: int = 0, y: int = 0, w: int = -1, h: int = -1,
             frozen_after_ms: int = 0, max_iter: int = 10) -> QImage:
        """Get a QImage of the page.

        If the image area is larger than the page, it is automatically truncated.

        A negative value for w or h means page width/height.

        :param frozen_after_ms: start a loop which stops when 2 captures
            separated by a time limit are identical or when max_iter is reached.
        :param max_iter: maximum number of iterations used with frozen_after_ms.
        """
        self.log(_LOG_CAT, 'grab:', (x, y, w, h), 'delay:', frozen_after_ms, 'max_iter:', max_iter)

        page_width, page_height, scroll_top, screen_h, scroll_left = self.execute_script(
            '''
            e = document.documentElement;
            return [e.scrollWidth, e.scrollHeight, e.scrollTop, e.clientHeight, window.scrollX];''')
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
                self.execute_script(f'window.scroll({scroll_left}, {y})')
                self.sleep_ms(50 + h//1000 * 20)

            rect = QRect(x, 0, w, h)
        else:
            rect = QRect(x, y - scroll_top, w, h)

        self.log(_LOG_CAT, 'computed rect:', rect)

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

            self.execute_script(f'window.scroll({scroll_left}, {scroll_top})')

            self.sleep_ms(200)

        return img1

    def take_screenshot(self, filename: str, format: Optional[bytes] = None,
                        quality: int = -1,
                        x: int = 0, y: int = 0, w: int = -1, h: int = -1,
                        frozen_after_ms: int = 0, max_iter: int = 10) -> bool:
        """Screenshot the page.

        See self.grab()
        """
        self.log(_LOG_CAT, 'screenshot:', filename, 'format:', format)
        img = self.grab(x, y, w, h, frozen_after_ms, max_iter)
        return img.save(filename, format, quality)

    def resize(self, width: int = -1, height: int = -1) -> None:
        """Resize view

        A negative value for width or height means contents size width/height.
        """
        self.log(_LOG_CAT, 'resize:', width, height)
        if width <= 0 or height <= 0:
            size = self._page.contentsSize().toSize()
            if width <= 0:
                width = size.width()
            if height <= 0:
                height = size.height()
            self.log(_LOG_CAT, 'resize(computed):', width, height)
        self._view.resize(width, height)

    def contents_size(self) -> tuple[int, int]:
        """Get contents size"""
        size = self._page.contentsSize().toSize()
        return (size.width(), size.height())

    def scroll(self, x: int, y: int) -> None:
        """Scroll at position"""
        self.execute_script(f'window.scroll({x},{y})')

    def click(self, x: int, y: int, delay_ms: int = 100) -> None:
        """Click at position"""
        for widget in self._view.children():
            if widget.isWidgetType():
                break

        pos = QPoint(x, y)
        if self._headless:
            # restore mouse event
            self._view.setAttribute(Qt.WA_ForceDisabled, False)

        _left_click(widget, QEvent.MouseButtonPress, pos)
        self.sleep_ms(delay_ms)
        _left_click(widget, QEvent.MouseButtonRelease, pos)

        if self._headless:
            self._view.setAttribute(Qt.WA_ForceDisabled)

    def enable_devtools(self, enable: bool = True) -> None:
        """Enable or disable devtools"""
        if enable:
            if not self._dev_view:
                self._dev_view = QWebEngineView()
                page = self._dev_view.page()
                self._page.setDevToolsPage(page)
                self._dev_view.show()
        elif self._dev_view:
            self._dev_view.deleteLater()
            self._dev_view = None
            self._page.setDevToolsPage(None)

    def _toggle_devtools(self):
        self.enable_devtools(not bool(self._dev_view))

    def _event_result(self, result):
        self._result = result
        self._event_loop.exit()

    def _event_load_finished(self, result):
        if self._enable_exit_event_on_load_finished:
            self._event_loop.exit()

    def _download_request(self, item: QWebEngineDownloadRequest):
        # TODO check url origin
        self._download_item = item
        item.isFinishedChanged.connect(self._download_finished)
        if self._with_progression:
            if not self._progress_timer:
                self._progress_timer = QTimer()
                self._progress_timer.timeout.connect(
                    lambda: self.log(_LOG_CAT, f'download {item.receivedBytes()}/{item.totalBytes()}'))
            self._progress_timer.start(1000)

        if self._downloaded_filename:
            item.setDownloadFileName(self._downloaded_filename)
            item.setSavePageFormat(self._downloaded_format)
            self._downloaded_filename = ''

        item.accept()
        filename = f'{item.downloadDirectory()}/{item.downloadFileName()}'
        self.log(_LOG_CAT, f'download to {filename}')

    def _download_finished(self):
        try:
            state = self._download_item.state()
            if state == QWebEngineDownloadRequest.DownloadCompleted:
                self.log(_LOG_CAT, 'download, done')
            elif state == QWebEngineDownloadRequest.DownloadInterrupted:
                msg = self._download_item.interruptReasonString()
                self.log(_LOG_CAT, msg)
                self._result = DownloadException(msg)
            elif state == QWebEngineDownloadRequest.DownloadCancelled:
                self.log(_LOG_CAT, 'download, cancelled')
            else:
                # InProgress, do nothing
                return
        except BaseException as e:
            self._result = e

        if self._progress_timer:
            self._progress_timer.stop()

        self._download_item = None
        self._event_loop.exit()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.quit()


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('requires a url', file=sys.stderr)
        exit(1)

    app = AppDriver(headless=False, logger=True)

    def run(driver: WebDriver):
        driver.get(sys.argv[1])
        driver.wait_quit()
    exit(app.run(run))
