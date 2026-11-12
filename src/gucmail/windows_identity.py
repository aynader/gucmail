"""Bind per-user tasks to the process's Windows token, without stored credentials."""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from pathlib import Path, PureWindowsPath

from gucmail.errors import ConfigurationError


def windowless_python(executable: str | Path) -> str:
    """Prefer ``pythonw.exe`` for background tasks so Windows opens no console window.

    Task Scheduler gives a console program (python.exe) a visible terminal even for a
    hidden task. The windowless twin ships next to every Windows interpreter and venv.
    """

    text = str(executable)
    path = PureWindowsPath(text)
    if path.name.casefold() == "python.exe":
        windowless = path.with_name("pythonw.exe")
        if Path(str(windowless)).is_file():
            return str(windowless)
    return text


def validate_windows_sid(value: str) -> str:
    if not re.fullmatch(r"S-1-\d{1,15}(?:-\d{1,10}){1,15}", value):
        raise ConfigurationError("A valid Windows user SID is required for this task.")
    # System/service accounts must not be a fallback for a per-user mail task.
    if value in {"S-1-5-18", "S-1-5-19", "S-1-5-20"}:
        raise ConfigurationError("Create the mail task from the intended Windows user account.")
    return value


def current_windows_user_sid() -> str:
    """Read TOKEN_USER from this process; close handles and free native allocations."""
    if os.name != "nt":
        raise ConfigurationError("Supply a Windows user SID when generating a Windows task here.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    security.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    text = wintypes.LPWSTR()
    try:
        if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        length = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not 0 < length.value <= 65536:
            raise OSError("Windows returned an invalid token buffer size.")
        buffer = ctypes.create_string_buffer(length.value)
        if not security.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is PSID.
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        return validate_windows_sid(text.value or "")
    except OSError as exc:
        raise ConfigurationError(
            "Could not determine this Windows user's identity. No task was generated."
        ) from exc
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        if token:
            kernel.CloseHandle(token)
