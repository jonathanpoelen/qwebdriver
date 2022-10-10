import sys
import atexit
import importlib
import multiprocessing
import signal
import threading
import traceback
from typing import Optional, Callable
from PySide6.QtCore import (QObject,
                            Slot,
                            Signal,
                            QThread,
                            SIGNAL,
                            SLOT)
from PySide6.QtGui import QImage
from . import webdriver


_LOG_CAT = '\x1b[34m[idriver]\x1b[0m'

_null_fn = webdriver._null_fn
_strerr_print = webdriver._strerr_print


def _select_logger(enable: bool):
    return _strerr_print if enable else _null_fn


"""
code.interact() must be running in the main process for autocomplete to work.

Qt's loop must run in a secondary process so that it is not blocked by the
python interpreter.

                 ┌────────────────────────────┐
                 │        Main Process        │
                 │ ┌────────────────────────┐ │
                 │ │  _InteractiveWebDriver │ │
                 │ │   ┌────────────────┐   │ │
                 │ │   │     Thread     │   │ │
                 │ │   │ _Interceptor ↔┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈╮
                 │ │   └────────────────┘   │ │                  ┊
                 │ │                        │ │                  ┊
                 │ │    def command(...) ←┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈╮      ┊
                 │ │           ↓            │ │           ┊      ┊
                 │ │   driver_chann:Pipe →┈┈┈┈┈┈┈┈┈┈╮     ┊      ┊
                 │ │                        │ │     ┊     ┊      ┊
                 │ └────────────────────────┘ │     ┊     ┊      ┊
                 └────────────────────────────┘     ┊     ┊      ┊
                                                    ┊     ┊      ┊
                 ┌─────────────────────────────┐  (cmd)   ┊      ┊
                 │      Secondary Process      │    ┊     ┊      ┊
                 │ ┌─────────────────────────┐ │    ┊  (result)  ┊
                 │ │      _Synchronizer      │ │    ┊     ┊      ┊
                 │ │   ┌──────────────────┐  │ │    ┊     ┊    (url)
                 │ │   │     QThread      │  │ │    ┊     ┊   (result)
                 │ │   │ _MessageWorker ←┈┈┈┈┈┈┈┈┈┈┈╯     ┊      ┊
                 │ │   └────────│─────────┘  │ │          ┊      ┊
                 │ │            ↓            │ │          ┊      ┊
                 │ │     Signal received     │ │          ┊      ┊
                 │ │            ↓            │ │          ┊      ┊
    ╭┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈↔ slot _sync_driver()   │ │          ┊      ┊
    ┊            │ │    driver_chann:Pipe →┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈╯      ┊
  (cmd)          │ │                         │ │                 ┊
 (result)  ╭┈┈┈┈┈┈┈┈↔ def _interceptor(url)  │ │                 ┊
    ┊      ┊     │ │ interceptor_chann:Pipe ↔┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈╯
    ┊      ┊     │ │                         │ │
    ┊    (url)   │ └─────────────────────────┘ │
    ┊   (result) │                             │
    ┊      ┊     │    ┌─────────────┐          │
    ┊      ╰┈┈┈┈┈┈┈┈┈↔│  AppDriver  │          │
    ╰┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈↔│  WebDriver  │          │
                 │    │   Qt Loop   │          │
                 │    └─────────────┘          │
                 └─────────────────────────────┘
"""


class _MessageWorker(QThread):
    received = Signal()
    alive = True

    def __init__(self, driver_chann: multiprocessing.Pipe, p=None):
        super().__init__(p)
        self.driver_chann = driver_chann

    def run(self):
        chann = self.driver_chann
        emit = self.received.emit
        while self.alive:
            self.data = chann.recv()
            emit()


class _Synchronizer(QObject):
    def __init__(self, app, driver_chann, interceptor_chann, logger):
        super().__init__()
        self.app = app
        self.log = logger
        self.driver = self.app.driver
        self.driver_chann = driver_chann
        self.interceptor_chann = interceptor_chann
        self.driver_messager = _MessageWorker(driver_chann, self.app._app)
        QObject.connect(self.driver_messager, SIGNAL('received()'), self, SLOT('_sync_driver()'))
        self.driver_messager.start()

    @Slot()
    def _sync_driver(self):
        self.log(_LOG_CAT, 'sync_driver:', self.driver_messager.data)
        method = self.driver_messager.data[0]
        try:
            if method == 'quit':
                self.driver_messager.alive = False
                self.interceptor_chann.send(None)
                self.driver_chann.close()
                self.interceptor_chann.close()
                self.app.quit()
            elif method == 'interceptor':
                if self.driver_messager.data[1]:
                    self.driver.set_url_request_interceptor(self._interceptor)
                else:
                    self.driver.set_url_request_interceptor(None)
                self.driver_chann.send((True, None))
            elif method == 'grab':
                args = self.driver_messager.data[1]
                img: QImage = getattr(self.driver, method)(*args)
                bits = img.constBits().tobytes()
                format = img.format()
                bytesPerLine = img.bytesPerLine()
                # TODO: support of large image (more 32Mo)
                result = (bits, img.width(), img.height(), bytesPerLine, int(format))
                self.driver_chann.send((True, result))
            else:
                args = self.driver_messager.data[1]
                result = getattr(self.driver, method)(*args)
                self.driver_chann.send((True, result))
        except Exception as ex:
            traceback.print_exc()
            self.driver_chann.send((False, type(ex).__name__, ex.args))

    def _interceptor(self, url):
        self.log(_LOG_CAT, 'interceptor:', url)
        self.interceptor_chann.send(url)
        return self.interceptor_chann.recv()


def _default_interceptor(url):
    return False


class _Interceptor:
    interceptor = _default_interceptor

    def __init__(self, interceptor_chann):
        self.interceptor_chann = interceptor_chann

    def run(self):
        chann = self.interceptor_chann
        while url := chann.recv():
            try:
                r = self.interceptor(url)
            except:  # noqa: E722
                traceback.print_exc(file=sys.stderr)
                r = False
            chann.send(r)
        chann.close()


class _InteractiveWebDriver:
    """Interactive WebDriver.

    The API is strictly identical to WebDriver.
    """

    _interceptor_thread = None
    _depth = 0

    def __init__(self, driver_chann, interceptor_chann, interceptor_chann_recv, logger):
        self.log = logger
        self._driver_chann = driver_chann
        self._interceptor_chann = interceptor_chann
        self._interceptor_chann_recv = interceptor_chann_recv
        self._interceptor = _Interceptor(interceptor_chann)

    def quit(self):
        if self._driver_chann:
            try:
                self._driver_chann.send(('quit',))
            except BrokenPipeError:
                # already closed
                pass
            self._driver_chann.close()
            if self._interceptor_thread:
                self._interceptor_chann_recv.send('')
                self._interceptor_thread.join()
            self._interceptor_chann.close()
            self._driver_chann = None

    def set_url_request_interceptor(self, interceptor: Optional[Callable[[str], bool]]) -> None:
        if not self._interceptor_thread:
            self._interceptor_thread = threading.Thread(target=self._interceptor.run)
            self._interceptor_thread.daemon = True
            self._interceptor_thread.start()
        self._interceptor.interceptor = interceptor or _default_interceptor
        self._exec('interceptor', bool(interceptor))

    def get(self, url: str) -> None:
        return self._exec('get', (url,))

    def sleep_ms(self, ms: int) -> None:
        return self._exec('sleep_ms', (ms,))

    def download(self, url: str, filename: str = None, with_progression: bool = True) -> None:
        return self._exec('download', (url, filename, with_progression,))

    def execute_script(self, script: str, raise_if_js_error: bool = True):
        res = self._exec('execute_script', (script, False,))
        if raise_if_js_error:
            error = self.get_last_js_error()
            if error:
                raise webdriver.JsException(error)
        return res

    def get_last_js_error(self) -> str:
        return self._exec('get_last_js_error', ())

    def grab(self, x=0, y=0, w=-1, h=-1, frozen_after_ms=0, max_iter=10) -> QImage:
        d = self._exec('grab', (x, y, w, h, frozen_after_ms, max_iter,))
        # copy for detached buffer
        img = QImage(d[0], d[1], d[2], d[3], QImage.Format(d[4]))
        img.convertTo(QImage.Format_RGB32)
        return img

    def take_screenshot(self, filename: str, format: Optional[str] = None, quality: int = -1,
                        x=0, y=0, w=-1, h=-1, frozen_after_ms=0, max_iter=10) -> bool:
        return self._exec('take_screenshot', (filename, format, quality,
                                              x, y, w, h, frozen_after_ms, max_iter))

    def resize(self, width: int = -1, height: int = -1) -> None:
        return self._exec('resize', (width, height,))

    def contents_size(self) -> tuple[int, int]:
        return self._exec('contents_size')

    def scroll(self, x: int, y: int) -> None:
        return self._exec('scroll', (x, y,))

    def enable_devtools(self, enable: bool = True) -> None:
        return self._exec('enable_devtools', (enable,))

    def _exec(self, mem, data=()):
        logsep = ' ' if self._depth else ''
        self.log(_LOG_CAT, f'{"":>>{self._depth}}{logsep}exec:', mem, data)

        self._depth += 1
        self._driver_chann.send((mem, data))
        res = self._driver_chann.recv()
        self._depth -= 1

        logprefix = f'{"":><{self._depth}}{logsep}'
        if mem == 'grab':
            self.log(_LOG_CAT, logprefix, mem, 'img.len=', len(res[1][0]), res[1][1:])
        else:
            self.log(_LOG_CAT, logprefix, mem, res)

        # is not an exception
        if res[0]:
            return res[1]

        # rebuild exception
        names = res[1].rsplit('.', 1)
        if len(names) == 1:
            class_name = names[0]
            module = globals()['__builtins__']
            class_ = module[class_name]
        else:
            class_name = names[1]
            module = importlib.import_module(names[0])
            class_ = getattr(module, class_name)

        raise class_(*res[2])


def _webdriver_process(driver_chann, interceptor_chann, debug, idebug):
    # ignore Ctrl+C
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    app = webdriver.AppDriver(headless=False, logger=debug)
    sync = _Synchronizer(app, driver_chann, interceptor_chann,  # noqa: F841
                         _select_logger(idebug))
    app.exec()


class AppDriver:
    def __init__(self, debug: bool = False, idebug: bool = False):
        driver_chann1, driver_chann2 = multiprocessing.Pipe()
        interceptor_chann1, interceptor_chann2 = multiprocessing.Pipe()
        self.p = multiprocessing.Process(target=_webdriver_process,
                                         args=(driver_chann1, interceptor_chann1,
                                               debug, idebug))
        self.p.start()

        self.driver = _InteractiveWebDriver(driver_chann2, interceptor_chann2,
                                            interceptor_chann1,
                                            _select_logger(idebug))
        atexit.register(lambda: self.quit())

    def run(self, f: Callable[[webdriver.WebDriver], None]) -> int:
        try:
            f(self.driver)
            return 0
        finally:
            if self.p.is_alive():
                self.quit()

    def quit(self):
        self.driver.quit()
        self.p.join()

    def __enter__(self):
        return self.driver

    def __exit__(self, type, value, traceback):
        self.quit()


if __name__ == '__main__':
    import rlcompleter  # noqa: F401
    import readline
    import code
    import argparse

    # function to limit the variables accessible in code.interact()
    def _init():
        history_file = 'webdriver_history'
        parser = argparse.ArgumentParser(
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description='Interactive web driver that starts with predefined variables:\n'
                        '- d and driver as WebDriver\n'
                        '- app like AppDriver')
        parser.add_argument('--debug', metavar='N', type=int, nargs='?', default=0,
                            help='0 = none, 1 = webdriver, 2 = interactive_webdriver, 3 = both')
        parser.add_argument('--history-file', metavar='PATH', type=str,
                            default=history_file, help=f'default: {history_file}')
        parser.add_argument('url', metavar='URL', type=str, nargs='?')

        args = parser.parse_args()

        # enable auto-completion
        readline.parse_and_bind('tab:complete')

        history_file = args.history_file
        if history_file:
            try:
                readline.read_history_file(history_file)
            except FileNotFoundError:
                pass

        debug = args.debug if args.debug else (3 if args.debug is None else 0)
        app = AppDriver(debug & 1, debug & 2)

        if args.url:
            app.driver.get(args.url)

        return app, history_file

    app, history_file = _init()

    # variables for code.interact()
    driver = app.driver
    d = driver

    code.interact(local=globals())

    if history_file:
        readline.write_history_file(history_file)
    app.quit()
