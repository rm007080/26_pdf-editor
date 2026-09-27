import ctypes
import queue
import threading
from collections.abc import Callable
from ctypes import wintypes

UNSAVED_CONFIRM = "未保存の変更があります。破棄して新しく始めますか？"
SPLIT_CONFIRM = "まだ書き出していない分割設定があります。破棄して新しく始めますか？"
BOTH_CONFIRM = "未保存の変更と、まだ書き出していない分割設定があります。破棄して新しく始めますか？"
MENU_OPEN = "開く"
MENU_QUIT = "終了"
ID_OPEN = 1
ID_QUIT = 2

_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_COMMAND = 0x0111
_WM_RBUTTONUP = 0x0205
_WM_CONTEXTMENU = 0x007B
_WM_USER = 0x0400
_WM_TRAY = _WM_USER + 20
_WM_APP_ASK = _WM_USER + 21
_WM_APP_REMOVE = _WM_USER + 22
_NIM_ADD = 0x00000000
_NIM_DELETE = 0x00000002
_NIF_MESSAGE = 0x00000001
_NIF_ICON = 0x00000002
_NIF_TIP = 0x00000004
_MF_STRING = 0x00000000
_TPM_RIGHTALIGN = 0x0008
_TPM_BOTTOMALIGN = 0x0020
_TPM_NONOTIFY = 0x0080
_TPM_RETURNCMD = 0x0100
_IDI_APPLICATION = 32512
_MB_YESNO = 0x00000004
_MB_ICONWARNING = 0x00000030
_IDYES = 6
_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


def loss_confirm_message(unsaved: bool, split_unexported: bool) -> str | None:
    if unsaved and split_unexported:
        return BOTH_CONFIRM
    if unsaved:
        return UNSAVED_CONFIRM
    if split_unexported:
        return SPLIT_CONFIRM
    return None


def menu_entries() -> tuple[tuple[str, int], ...]:
    return ((MENU_OPEN, ID_OPEN), (MENU_QUIT, ID_QUIT))


def decide_quit(
    *,
    dirty: bool,
    holder: str | None,
    stop: Callable[[], None],
    begin_exit: Callable[[], None],
    ask: Callable[[str], None],
    message: str | None,
) -> None:
    if not dirty:
        stop()
        return
    if holder is not None:
        begin_exit()
        return
    if message is None:
        stop()
        return
    ask(message)


def message_box(hwnd: int, text: str) -> bool:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user32.MessageBoxW.restype = ctypes.c_int
    result = user32.MessageBoxW(hwnd, text, "PDF編集", _MB_YESNO | _MB_ICONWARNING)
    return result == _IDYES


class Tray:
    def __init__(self, on_open: Callable[[], None], on_quit: Callable[[], None]) -> None:
        self._on_open = on_open
        self._on_quit = on_quit
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._hwnd = 0
        self._closed = threading.Event()
        self._ready = threading.Event()
        self._gone = threading.Event()
        self._wndproc = None
        self._error: BaseException | None = None
        self.removed = False

    def start(self, *, window: bool = True) -> None:
        target = self._run_window if window else self._run_queue
        self._thread = threading.Thread(target=target, name="pdf-editor-tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("トレイを開始できませんでした")
        if self._error is not None:
            raise self._error

    @property
    def thread_ident(self) -> int | None:
        thread = self._thread
        if thread is None:
            return None
        return thread.ident

    def handle_command(self, command_id: int) -> None:
        if command_id == ID_OPEN:
            self._on_open()
        elif command_id == ID_QUIT:
            self._on_quit()

    def ask(self, text: str, on_yes: Callable[[], None], on_no: Callable[[], None]) -> None:
        # 要求スレッドではモーダルにしない。隠しウィンドウのスレッドが MessageBoxW を出す。
        self._queue.put(("ask", text, on_yes, on_no))
        hwnd = self._hwnd
        if hwnd:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.PostMessageW.restype = wintypes.BOOL
            user32.PostMessageW(hwnd, _WM_APP_ASK, 0, 0)

    def remove(self) -> None:
        if self.removed:
            return
        self.removed = True
        if self._thread is not None and threading.current_thread() is self._thread:
            self._finish_remove()
            return
        hwnd = self._hwnd
        if hwnd:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.PostMessageW.restype = wintypes.BOOL
            user32.PostMessageW(hwnd, _WM_APP_REMOVE, 0, 0)
        else:
            self._queue.put(("remove",))
        self._gone.wait(2)

    def _finish_remove(self) -> None:
        self._delete_icon()
        self._closed.set()
        self._gone.set()
        hwnd = self._hwnd
        if not hwnd:
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.DestroyWindow(hwnd)

    def _run_queue(self) -> None:
        self._ready.set()
        while not self._closed.is_set():
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            self._dispatch(item)

    def _dispatch(self, item: tuple) -> None:
        kind = item[0]
        if kind == "ask":
            _, text, on_yes, on_no = item
            if message_box(self._hwnd, text):
                on_yes()
            else:
                on_no()
        elif kind == "remove":
            self._finish_remove()

    def _drain_asks(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            self._dispatch(item)

    def _run_window(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = _LRESULT

        @ _WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == _WM_TRAY:
                mouse = int(lparam) & 0xFFFF
                if mouse in {_WM_RBUTTONUP, _WM_CONTEXTMENU}:
                    self._show_menu(hwnd)
                return 0
            if msg == _WM_COMMAND:
                self.handle_command(int(wparam) & 0xFFFF)
                return 0
            if msg == _WM_APP_ASK:
                self._drain_asks()
                return 0
            if msg == _WM_APP_REMOVE:
                self._finish_remove()
                return 0
            if msg == _WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = wndproc
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        instance = kernel32.GetModuleHandleW(None)
        class_name = f"PdfEditorTray{id(self)}"

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        window_class = WNDCLASSW()
        window_class.lpfnWndProc = wndproc
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.RegisterClassW(ctypes.byref(window_class))
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        hwnd = user32.CreateWindowExW(0, class_name, "PDF編集", 0, 0, 0, 0, 0, None, None, instance, None)
        if not hwnd:
            self._error = ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            return
        self._hwnd = hwnd
        self._add_icon(hwnd)
        self._ready.set()
        message = wintypes.MSG()
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = _LRESULT
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def _show_menu(self, hwnd: int) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreatePopupMenu.argtypes = []
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT, wintypes.LPCWSTR]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            ctypes.c_void_p,
        ]
        user32.TrackPopupMenu.restype = wintypes.BOOL
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyMenu.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        menu = user32.CreatePopupMenu()
        for label, command_id in menu_entries():
            user32.AppendMenuW(menu, _MF_STRING, command_id, label)
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(hwnd)
        command = user32.TrackPopupMenu(
            menu,
            _TPM_RIGHTALIGN | _TPM_BOTTOMALIGN | _TPM_RETURNCMD | _TPM_NONOTIFY,
            point.x,
            point.y,
            0,
            hwnd,
            None,
        )
        user32.PostMessageW(hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)
        if command:
            self.handle_command(int(command))

    def _add_icon(self, hwnd: int) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        user32.LoadIconW.restype = wintypes.HICON
        icon = user32.LoadIconW(None, ctypes.cast(_IDI_APPLICATION, wintypes.LPCWSTR))

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
            ]

        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = hwnd
        data.uID = 1
        data.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        data.uCallbackMessage = _WM_TRAY
        data.hIcon = icon
        data.szTip = "PDF編集"
        self._nid = data
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data))

    def _delete_icon(self) -> None:
        data = getattr(self, "_nid", None)
        if data is None:
            return
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(type(data))]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
        self._nid = None
