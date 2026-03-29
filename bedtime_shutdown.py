import argparse
import atexit
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import signal
import subprocess
import sys
import os
import time
import threading
from typing import Callable

from CriticalProcess import criticalprocess

DEBUG = False

_DEFAULT_CONFIG = "bedtime_config.json"
_TIME_KEY = "shutdown_time"
_ADDICT_MODE_KEY = "addict_mode"
_LOG_FILE_KEY = "log_file"
_LOG_FILE_FROM_CONFIG = "__FROM_CONFIG__"

_ADDICT_WINDOW = timedelta(hours=4)
_ADDICT_SHUTDOWN_DELAY = timedelta(minutes=10)

_release_critical_ref: Callable[[], None] | None = None
_console_handlers: list[object] = []
_wndproc_ref: object | None = None
_hidden_window_ready = threading.Event()
_hidden_window_error: list[Exception] = []
_hidden_hwnd: wintypes.HWND | None = None
_message_thread: threading.Thread | None = None
_class_name: str | None = None

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010

_lresult_type = getattr(wintypes, "LRESULT", None)
if _lresult_type is None:
    _lresult_type = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long

_logger = logging.getLogger("bedtime_shutdown")


def _configure_file_logging(log_file: str | None) -> None:
    if not log_file:
        return
    log_path = Path(log_file)
    if log_path.parent and not log_path.parent.exists():
        log_path.parent.mkdir(parents=True, exist_ok=True)

    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    if _logger.handlers:
        _logger.handlers.clear()

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    _logger.addHandler(handler)
    _logger.info("File logging enabled: %s", log_path)


def _message(text: str, level: int = logging.INFO) -> None:
    print(text)
    if _logger.handlers:
        _logger.log(level, text)


def _mark_process_critical() -> Callable[[], None]:
    """Elevate privileges and mark the current process as critical."""
    global _release_critical_ref
    criticalprocess.critical_process()

    released = False

    def _clear_critical() -> None:
        nonlocal released
        if released:
            return
        released = True
        was_critical = criticalprocess.wintypes.BOOLEAN(0)
        criticalprocess.RtlSetProcessIsCritical(False, ctypes.byref(was_critical), False)
        _message("Critical status removed.")

    atexit.register(_clear_critical)
    _message(f"Running critical process with PID: {os.getpid()}...")
    _release_critical_ref = _clear_critical
    return _clear_critical


def _install_console_handler() -> None:
    """Ensure Windows console control events release critical status."""
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def _console_handler(event: int) -> bool:
        if event in (0, 1, 2, 5, 6):  # CTRL_C, CTRL_BREAK, CLOSE, LOGOFF, SHUTDOWN
            if _release_critical_ref is not None:
                _release_critical_ref()
        return False

    c_handler = handler_type(_console_handler)
    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(c_handler, True):
        raise ctypes.WinError()
    _console_handlers.append(c_handler)

    def _remove_handler() -> None:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(c_handler, False)

    atexit.register(_remove_handler)


def _install_session_message_window() -> None:
    """Create a hidden window to capture session-ending messages."""
    global _wndproc_ref, _message_thread, _class_name, _hidden_hwnd

    if _message_thread and _message_thread.is_alive():
        return

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = _lresult_type

    wndproc_type = ctypes.WINFUNCTYPE(_lresult_type, wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", wndproc_type),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    def _thread_target() -> None:
        global _hidden_hwnd, _class_name, _wndproc_ref
        try:
            def _wndproc(hwnd: wintypes.HWND, msg: int, wparam: int, lparam: int) -> int:
                if msg in (WM_QUERYENDSESSION, WM_ENDSESSION):
                    if _release_critical_ref is not None:
                        _release_critical_ref()
                    if msg == WM_QUERYENDSESSION:
                        return 1
                elif msg == WM_CLOSE:
                    user32.DestroyWindow(hwnd)
                    return 0
                elif msg == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            wndproc = wndproc_type(_wndproc)
            _wndproc_ref = wndproc

            hinstance = kernel32.GetModuleHandleW(None)
            class_name = f"BedtimeShutdownHiddenWindow_{os.getpid()}"
            _class_name = class_name

            wndclass = WNDCLASS()
            wndclass.style = 0
            wndclass.lpfnWndProc = wndproc
            wndclass.cbClsExtra = 0
            wndclass.cbWndExtra = 0
            wndclass.hInstance = hinstance
            wndclass.hIcon = None
            wndclass.hCursor = None
            wndclass.hbrBackground = None
            wndclass.lpszMenuName = None
            wndclass.lpszClassName = class_name

            atom = user32.RegisterClassW(ctypes.byref(wndclass))
            if not atom:
                raise ctypes.WinError()

            hwnd = user32.CreateWindowExW(0, class_name, "BedtimeShutdownHidden", 0,
                                          0, 0, 0, 0, None, None, hinstance, None)
            if not hwnd:
                raise ctypes.WinError()

            _hidden_hwnd = wintypes.HWND(hwnd)
            _hidden_window_ready.set()

            msg = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        except Exception as exc:  # noqa: BLE001
            _hidden_window_error.append(exc)
            _hidden_window_ready.set()
        finally:
            if _hidden_hwnd and user32.IsWindow(_hidden_hwnd):
                user32.DestroyWindow(_hidden_hwnd)
            _hidden_hwnd = None
            if _class_name:
                user32.UnregisterClassW(_class_name, kernel32.GetModuleHandleW(None))
                _class_name = None

    _hidden_window_ready.clear()
    _hidden_window_error.clear()

    thread = threading.Thread(target=_thread_target, name="BedtimeShutdownHiddenWnd", daemon=True)
    _message_thread = thread
    thread.start()

    if not _hidden_window_ready.wait(timeout=5):
        raise RuntimeError("Timed out creating hidden session window.")
    if _hidden_window_error:
        raise _hidden_window_error[0]

    def _shutdown_window_cleanup() -> None:
        if _hidden_hwnd:
            user32.PostMessageW(_hidden_hwnd, WM_CLOSE, 0, 0)

    atexit.register(_shutdown_window_cleanup)

def _handle_signal(signum: int, frame: object | None) -> None:
    _message("Received interrupt; exiting cleanly.")
    if _release_critical_ref is not None:
        _release_critical_ref()
    sys.exit(0)


def _parse_time_string(time_str: str) -> datetime:
    now = datetime.now()
    try:
        target_time = datetime.strptime(time_str, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("Time must be provided in HH:MM 24-hour format.") from exc

    target = now.replace(hour=target_time.hour, minute=target_time.minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _load_config(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found at {config_path}.")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Config file {config_path} contains invalid JSON.") from exc

    if not isinstance(data, dict):
        raise ValueError("Config file must contain a JSON object.")

    if _TIME_KEY not in data:
        raise ValueError(f"Config file must include a '{_TIME_KEY}' entry.")

    return data


def _config_time(data: dict[str, object]) -> str:
    value = data[_TIME_KEY]
    if not isinstance(value, str):
        raise ValueError(f"'{_TIME_KEY}' must be a string in HH:MM format.")
    return value


def _config_addict_mode(data: dict[str, object]) -> bool:
    value = data.get(_ADDICT_MODE_KEY, False)
    if not isinstance(value, bool):
        raise ValueError(f"'{_ADDICT_MODE_KEY}' must be true or false.")
    return value


def _config_log_file(data: dict[str, object]) -> str | None:
    value = data.get(_LOG_FILE_KEY, None)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"'{_LOG_FILE_KEY}' must be a string path or null.")
    if not value.strip():
        return None
    return value


def _format_delta(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _wait_until(target: datetime) -> None:
    while True:
        now = datetime.now()
        if now >= target:
            return
        remaining = target - now
        _message(f"Time until shutdown: {_format_delta(remaining)}")
        sleep_for = min(remaining.total_seconds(), 60)
        time.sleep(max(1, sleep_for))


def _run_shutdown(force: bool) -> None:
    command = ["shutdown", "/s", "/t", "0"]
    if force:
        command.append("/f")
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Shutdown command failed.") from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Schedule a forced Windows shutdown at a configured time.")
    parser.add_argument("--time", "-t", dest="time_str", help="Shutdown time in HH:MM 24-hour format.")
    parser.add_argument("--config", "-c", dest="config", default=_DEFAULT_CONFIG,
                        help=f"Path to JSON config file (default: {_DEFAULT_CONFIG}).")
    parser.add_argument("--addict-mode", dest="addict_mode", action="store_true",
                        help="Enable addict mode override (shutdown in 10 minutes when started within 4 hours of bedtime).")
    parser.add_argument("--no-addict-mode", dest="addict_mode", action="store_false",
                        help="Disable addict mode override, even if config enables it.")
    parser.add_argument("--log-file", dest="log_file",
                        help="Write status output to this log file path.")
    parser.add_argument("--no-log-file", dest="log_file", action="store_const", const=None,
                        help="Disable file logging, even if config enables it.")
    parser.add_argument("--no-force", dest="force", action="store_false",
                        help="Do not force-close running applications during shutdown.")
    parser.set_defaults(addict_mode=None)
    parser.set_defaults(log_file=_LOG_FILE_FROM_CONFIG)
    parser.set_defaults(force=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    config_path = Path(args.config)
    config_data: dict[str, object] = {}
    if config_path.exists():
        config_data = _load_config(config_path)
    elif args.time_str is None or args.addict_mode is None or args.log_file == _LOG_FILE_FROM_CONFIG:
        raise FileNotFoundError(f"Config file not found at {config_path}.")

    time_str = args.time_str or _config_time(config_data)
    addict_mode = _config_addict_mode(config_data) if args.addict_mode is None else args.addict_mode
    log_file = _config_log_file(config_data) if args.log_file == _LOG_FILE_FROM_CONFIG else args.log_file

    _configure_file_logging(log_file)

    target = _parse_time_string(time_str)
    if addict_mode:
        until_bedtime = target - datetime.now()
        if until_bedtime <= _ADDICT_WINDOW:
            target = datetime.now() + _ADDICT_SHUTDOWN_DELAY
            _message("Addict mode active: started within 4 hours of bedtime, shutdown forced in 10 minutes.")

    _message(f"Shutdown scheduled for {target.strftime('%Y-%m-%d %H:%M:%S')} ({_format_delta(target - datetime.now())} from now).")

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_signal)  # type: ignore[arg-type]

    release_critical = _mark_process_critical()
    _install_console_handler()
    _install_session_message_window()

    if DEBUG:
        # create a popup to inform the user
        MessageBox = ctypes.windll.user32.MessageBoxW
        MessageBox(None, f"System will shut down at {target.strftime('%H:%M')}.\nPress OK to continue.", "Bedtime Shutdown", 0)

    try:
        _wait_until(target)
        _message("Initiating system shutdown...")
        release_critical()  # Drop critical status before shutting down.
        _run_shutdown(force=args.force)
    except KeyboardInterrupt:
        _message("Interrupted before shutdown.")
    except Exception as exc:
        _message(f"Error: {exc}", level=logging.ERROR)
        sys.exit(1)


if __name__ == "__main__":
    main()
