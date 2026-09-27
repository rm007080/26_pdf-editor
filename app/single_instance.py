import ctypes
import os
import sys
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

MUTEX_NAME = "Local\\pdf-editor"
TOKEN_DIR = "pdf-editor"
TOKEN_NAME = "session.token"
OPEN_URL = "http://127.0.0.1:8765/api/editor/open"
PORT_BUSY_MESSAGE = "ポート 8765 を使用できないため終了します。"
OPEN_FAILED_MESSAGE = "既に起動している画面を開けなかったため終了します。"
_ERROR_ALREADY_EXISTS = 183
_mutex_handle: wintypes.HANDLE | None = None


def token_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA がありません")
    return Path(local) / TOKEN_DIR / TOKEN_NAME


def acquire_mutex(name: str = MUTEX_NAME) -> bool:
    global _mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetLastError.argtypes = [wintypes.DWORD]
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def write_session_token(token: str) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    _write_private(path, token.encode("utf-8"))


def read_session_token() -> str | None:
    try:
        text = token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    return text


def delete_session_token() -> None:
    try:
        token_path().unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def signal_running_instance(token: str, url: str = OPEN_URL) -> bool:
    request = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def finish_as_second_instance() -> int:
    token = read_session_token()
    if token is None:
        print(PORT_BUSY_MESSAGE, file=sys.stderr)
        return 1
    if signal_running_instance(token):
        return 0
    print(OPEN_FAILED_MESSAGE, file=sys.stderr)
    return 1


def _current_user_sid() -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        sid = ctypes.cast(buffer, ctypes.POINTER(SID_AND_ATTRIBUTES)).contents.Sid
        text = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            value = text.value
        finally:
            kernel32.LocalFree(text)
        if not value:
            raise OSError("現在のユーザーを特定できませんでした")
        return value
    finally:
        kernel32.CloseHandle(token)


def _write_private(path: Path, data: bytes) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    # 保護された DACL で、現在のユーザー以外の ACE を付けない。
    sddl = f"D:P(A;;FA;;;{_current_user_sid()})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(descriptor),
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    attributes = SECURITY_ATTRIBUTES()
    attributes.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    attributes.lpSecurityDescriptor = descriptor
    attributes.bInheritHandle = False
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value
    handle = kernel32.CreateFileW(
        str(path),
        0x40000000,
        0,
        ctypes.byref(attributes),
        1,
        0x80,
        None,
    )
    if handle == invalid or not handle:
        error = ctypes.get_last_error()
        kernel32.LocalFree(descriptor)
        raise ctypes.WinError(error)
    buffer = ctypes.create_string_buffer(data)
    written = wintypes.DWORD()
    ok = kernel32.WriteFile(handle, buffer, len(data), ctypes.byref(written), None)
    error = ctypes.get_last_error()
    kernel32.CloseHandle(handle)
    kernel32.LocalFree(descriptor)
    if not ok or written.value != len(data):
        raise ctypes.WinError(error)
