import ctypes
import queue
import threading
from ctypes import wintypes
from pathlib import Path

# ダイアログは、HTTP を処理するスレッドではなく COM を初期化した STA スレッドで出す。
_COINIT_APARTMENTTHREADED = 0x2
_OFN_HIDEREADONLY = 0x00000004
_OFN_NOCHANGEDIR = 0x00000008
_OFN_PATHMUSTEXIST = 0x00000800
_OFN_EXPLORER = 0x00080000

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
    # 上書き確認は出さない。同名は呼び出し側が (1), (2) で空ける。
    if comdlg32.GetSaveFileNameW(ctypes.byref(ofn)):
        return Path(file_buffer.value)
    if comdlg32.CommDlgExtendedError() == 0:
        return None
    raise OSError("保存ダイアログを開けませんでした")
