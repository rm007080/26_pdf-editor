import ctypes
import queue
import threading
from ctypes import wintypes
from pathlib import Path

# ダイアログは、HTTP を処理するスレッドではなく COM を初期化した STA スレッドで出す。
_COINIT_APARTMENTTHREADED = 0x2
_OFN_HIDEREADONLY = 0x00000004
_OFN_NOCHANGEDIR = 0x00000008
_OFN_ALLOWMULTISELECT = 0x00000200
_OFN_PATHMUSTEXIST = 0x00000800
_OFN_FILEMUSTEXIST = 0x00001000
_OFN_EXPLORER = 0x00080000
_OFN_ENABLEHOOK = 0x00000020
_WM_INITDIALOG = 0x0110
_WM_NOTIFY = 0x004E
_WM_CLOSE = 0x0010
_CDN_INITDONE = (0 - 601) & 0xFFFFFFFF
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_POPUP = 0x80000000
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_SHOWWINDOW = 0x0040
_SW_SHOW = 5
_SPI_GETWORKAREA = 0x0030
_HWND_TOPMOST = -1

_sta_queue: queue.Queue = queue.Queue()
_sta_lock = threading.Lock()
_sta_started = False


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


# FOLDERID_Documents
_DOCUMENTS = GUID(
    0xFDD39AD0,
    0x238F,
    0x46AF,
    (wintypes.BYTE * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7),
)


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", wintypes.LPARAM),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


def documents_folder() -> Path:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.HRESULT
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    pointer = ctypes.c_wchar_p()
    shell32.SHGetKnownFolderPath(ctypes.byref(_DOCUMENTS), 0, None, ctypes.byref(pointer))
    try:
        if not pointer.value:
            raise OSError("ドキュメントフォルダを取得できませんでした")
        return Path(pointer.value)
    finally:
        if pointer:
            ole32.CoTaskMemFree(pointer)


def ask_save_path(initial_dir: Path, initial_filename: str) -> Path | None:
    return _call_on_sta(_show_save_dialog, initial_dir, initial_filename)


def ask_folder(initial_dir: Path) -> Path | None:
    return _call_on_sta(_show_folder_dialog, initial_dir)


def ask_open_pdfs(initial_dir: Path) -> list[Path] | None:
    return _call_on_sta(_show_open_dialog, initial_dir)


def _ensure_sta() -> None:
    global _sta_started
    with _sta_lock:
        if _sta_started:
            return
        threading.Thread(target=_sta_main, name="pdf-editor-sta", daemon=True).start()
        _sta_started = True


def _sta_main() -> None:
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    try:
        while True:
            item = _sta_queue.get()
            if item is None:
                return
            func, args, done = item
            try:
                done.put((True, func(*args)))
            except Exception as exc:
                done.put((False, exc))
    finally:
        ole32.CoUninitialize()


def _call_on_sta(func, *args):
    _ensure_sta()
    done: queue.Queue = queue.Queue()
    _sta_queue.put((func, args, done))
    ok, value = done.get()
    if not ok:
        raise value
    return value


class _DialogWindows:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hwnd: int | None = None
        self._kind: str | None = None

    def remember(self, kind: str, hwnd: int) -> None:
        with self._lock:
            self._kind = kind
            self._hwnd = hwnd

    def clear(self, kind: str) -> None:
        with self._lock:
            if self._kind == kind:
                self._hwnd = None
                self._kind = None

    def cancel(self, kind: str) -> bool:
        with self._lock:
            if self._kind != kind or not self._hwnd:
                return False
            hwnd = self._hwnd
        _post_close(hwnd)
        return True


_dialogs = _DialogWindows()
_showing_kind: str | None = None


class _NMHDR(ctypes.Structure):
    _fields_ = [
        ("hwndFrom", wintypes.HWND),
        ("idFrom", ctypes.c_size_t),
        ("code", wintypes.UINT),
    ]


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
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
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    return user32


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return kernel32


def _post_close(hwnd: int) -> None:
    _user32().PostMessageW(hwnd, _WM_CLOSE, 0, 0)


def cancel_open_dialog() -> bool:
    return _dialogs.cancel("open")


def _dialog_window(child: int) -> int:
    parent = _user32().GetParent(child)
    return int(parent or child)


def _notify_code(lparam: int) -> int | None:
    if not lparam:
        return None
    return int(_NMHDR.from_address(lparam).code)


def _place_in_front(hwnd: int) -> None:
    user32 = _user32()
    dialog = wintypes.RECT()
    work = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(dialog))
    user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(work), 0)
    width = dialog.right - dialog.left
    height = dialog.bottom - dialog.top
    flags = _SWP_SHOWWINDOW
    x = 0
    y = 0
    if width > 0 and height > 0:
        x = work.left + max(0, ((work.right - work.left) - width) // 2)
        y = work.top + max(0, ((work.bottom - work.top) - height) // 2)
    else:
        flags |= _SWP_NOMOVE
    # バックグラウンドのプロセスは前面を取れない。最前面にして、ブラウザの裏に残さない。
    user32.SetWindowPos(hwnd, ctypes.c_void_p(_HWND_TOPMOST), x, y, 0, 0, flags | _SWP_NOSIZE)
    user32.ShowWindow(hwnd, _SW_SHOW)


def _force_foreground(hwnd: int) -> None:
    if not hwnd:
        return
    user32 = _user32()
    kernel32 = _kernel32()
    # Alt を押した扱いにすると、背面プロセスでも SetForegroundWindow が許される。
    user32.keybd_event(0x12, 0, 0, 0)
    try:
        foreground = user32.GetForegroundWindow()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        current = kernel32.GetCurrentThreadId()
        attached = False
        if foreground_thread and foreground_thread != current:
            attached = bool(user32.AttachThreadInput(current, foreground_thread, True))
        try:
            _place_in_front(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current, foreground_thread, False)
    finally:
        user32.keybd_event(0x12, 0, 0x0002, 0)


def _on_file_dialog(hwnd, msg, _wparam, lparam) -> int:
    # 前面化に失敗しても、ファイル選択そのものは開いたままにする。
    try:
        noticed = msg == _WM_NOTIFY and _notify_code(int(lparam or 0)) == _CDN_INITDONE
        if msg == _WM_INITDIALOG or noticed:
            window = _dialog_window(int(hwnd))
            if _showing_kind:
                _dialogs.remember(_showing_kind, window)
            _force_foreground(window)
    except Exception:
        return 0
    return 0


_file_dialog_hook = ctypes.WINFUNCTYPE(
    ctypes.c_size_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)(_on_file_dialog)


def _popup_owner() -> int:
    hwnd = _user32().CreateWindowExW(
        _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW,
        "Static",
        None,
        _WS_POPUP,
        0,
        0,
        0,
        0,
        None,
        None,
        None,
        None,
    )
    return int(hwnd or 0)


def _prepare_file_dialog(ofn: OPENFILENAMEW, kind: str) -> int:
    global _showing_kind
    ofn.Flags |= _OFN_ENABLEHOOK
    ofn.lpfnHook = ctypes.cast(_file_dialog_hook, ctypes.c_void_p)
    owner = _popup_owner()
    if owner:
        ofn.hwndOwner = owner
    _showing_kind = kind
    return owner


def _finish_file_dialog(kind: str, owner: int) -> None:
    global _showing_kind
    _dialogs.clear(kind)
    _showing_kind = None
    if owner:
        _user32().DestroyWindow(owner)


def _show_save_dialog(initial_dir: Path, initial_filename: str) -> Path | None:
    comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
    comdlg32.GetSaveFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    comdlg32.GetSaveFileNameW.restype = wintypes.BOOL
    comdlg32.CommDlgExtendedError.argtypes = []
    comdlg32.CommDlgExtendedError.restype = wintypes.DWORD

    file_buffer = ctypes.create_unicode_buffer(32768)
    file_buffer.value = initial_filename
    title = "書き出す PDF の保存先"
    extension = "pdf"
    file_filter = "PDF\0*.pdf\0"
    flags = _OFN_EXPLORER | _OFN_PATHMUSTEXIST | _OFN_NOCHANGEDIR | _OFN_HIDEREADONLY
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.lpstrFilter = file_filter
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.cast(file_buffer, wintypes.LPWSTR)
    ofn.nMaxFile = len(file_buffer)
    ofn.lpstrInitialDir = str(initial_dir)
    ofn.lpstrTitle = title
    ofn.Flags = flags
    ofn.lpstrDefExt = extension
    owner = _prepare_file_dialog(ofn, "save")
    try:
        # 上書き確認は出さない。同名は呼び出し側が (1), (2) で空ける。
        if comdlg32.GetSaveFileNameW(ctypes.byref(ofn)):
            return Path(file_buffer.value)
        if comdlg32.CommDlgExtendedError() == 0:
            return None
        raise OSError("保存ダイアログを開けませんでした")
    finally:
        _finish_file_dialog("save", owner)


def _show_open_dialog(initial_dir: Path) -> list[Path] | None:
    comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
    comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    comdlg32.GetOpenFileNameW.restype = wintypes.BOOL
    comdlg32.CommDlgExtendedError.argtypes = []
    comdlg32.CommDlgExtendedError.restype = wintypes.DWORD

    file_buffer = ctypes.create_unicode_buffer(65536)
    file_filter = ctypes.create_unicode_buffer("PDF\0*.pdf\0\0")
    flags = (
        _OFN_EXPLORER
        | _OFN_PATHMUSTEXIST
        | _OFN_FILEMUSTEXIST
        | _OFN_ALLOWMULTISELECT
        | _OFN_NOCHANGEDIR
        | _OFN_HIDEREADONLY
    )
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.lpstrFilter = ctypes.cast(file_filter, wintypes.LPCWSTR)
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.cast(file_buffer, wintypes.LPWSTR)
    ofn.nMaxFile = len(file_buffer)
    ofn.lpstrInitialDir = str(initial_dir)
    ofn.lpstrTitle = "PDF を選ぶ"
    ofn.Flags = flags
    ofn.lpstrDefExt = "pdf"
    owner = _prepare_file_dialog(ofn, "open")
    try:
        if not comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            if comdlg32.CommDlgExtendedError() == 0:
                return None
            raise OSError("ファイル選択を完了できませんでした")
        return _paths_from_open_buffer(file_buffer)
    finally:
        _finish_file_dialog("open", owner)


def _paths_from_open_buffer(buffer: ctypes.Array) -> list[Path]:
    parts: list[str] = []
    chunk: list[str] = []
    for char in buffer:
        if char == "\0":
            if not chunk:
                break
            parts.append("".join(chunk))
            chunk = []
            continue
        chunk.append(char)
    if not parts:
        return []
    if len(parts) == 1:
        return [Path(parts[0])]
    directory = Path(parts[0])
    return [directory / name for name in parts[1:]]


# BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX
_BIF_FOLDER = 0x0001 | 0x0040 | 0x0010
_BFFM_INITIALIZED = 1
_BFFM_SETSELECTIONW = 0x0400 + 103

_BrowseCallback = ctypes.WINFUNCTYPE(
    ctypes.c_int, wintypes.HWND, ctypes.c_uint, wintypes.LPARAM, wintypes.LPARAM
)


def _on_browse(hwnd, msg, _lparam, data) -> int:
    if msg == _BFFM_INITIALIZED:
        window = int(hwnd)
        _dialogs.remember("folder", window)
        _force_foreground(window)
        if data:
            user32 = _user32()
            user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.SendMessageW.restype = wintypes.LPARAM
            user32.SendMessageW(hwnd, _BFFM_SETSELECTIONW, 1, data)
    return 0


_browse_callback = _BrowseCallback(_on_browse)


class BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", ctypes.c_void_p),
        ("lParam", wintypes.LPARAM),
        ("iImage", ctypes.c_int),
    ]


def _show_folder_dialog(initial_dir: Path) -> Path | None:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    display = ctypes.create_unicode_buffer(260)
    initial = ctypes.create_unicode_buffer(str(initial_dir))
    info = BROWSEINFOW()
    owner = _popup_owner()
    info.hwndOwner = owner
    info.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
    info.lpszTitle = "分割した PDF の保存先フォルダ"
    info.ulFlags = _BIF_FOLDER
    info.lpfn = ctypes.cast(_browse_callback, ctypes.c_void_p)
    info.lParam = ctypes.cast(initial, ctypes.c_void_p).value or 0
    pidl = None
    try:
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
    finally:
        _dialogs.clear("folder")
        if owner:
            _user32().DestroyWindow(owner)
    if not pidl:
        return None
    try:
        path_buffer = ctypes.create_unicode_buffer(32768)
        if not shell32.SHGetPathFromIDListW(pidl, path_buffer):
            return None
        return Path(path_buffer.value)
    finally:
        ole32.CoTaskMemFree(pidl)
