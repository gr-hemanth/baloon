"""Win32 Process Spawner on Interactive WinSta0\\Default Desktop.

Guarantees that child processes (and their Chromium windows) are attached
to the physical interactive desktop monitor rather than non-interactive
agent / service desktops.
"""

import ctypes
from ctypes import wintypes
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger("desktop_spawner")

if sys.platform == "win32":
    kernel32 = ctypes.windll.kernel32

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_char_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]


def get_current_desktop_name() -> str:
    """Return the name of the current thread's desktop."""
    if sys.platform != "win32":
        return "Default"
    try:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        hdesk = u32.GetThreadDesktop(k32.GetCurrentThreadId())
        buf = ctypes.create_unicode_buffer(256)
        u32.GetUserObjectInformationW(hdesk, 2, buf, 512, None)
        return buf.value
    except Exception as exc:
        logger.warning("Could not query thread desktop: %s", exc)
        return "Default"


def is_on_default_desktop() -> bool:
    """Return True if currently executing on the physical interactive desktop."""
    return get_current_desktop_name() == "Default"


def spawn_process_on_default_desktop(cmd: str) -> int:
    """Launch a command string explicitly attached to WinSta0\\Default."""
    if sys.platform != "win32":
        import subprocess
        p = subprocess.Popen(cmd, shell=True)
        return p.pid

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.lpDesktop = "WinSta0\\Default"

    pi = PROCESS_INFORMATION()

    CREATE_NEW_CONSOLE = 0x00000010
    success = kernel32.CreateProcessW(
        None,
        cmd,
        None,
        None,
        False,
        CREATE_NEW_CONSOLE,
        None,
        None,
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    if not success:
        err = kernel32.GetLastError()
        raise RuntimeError(f"CreateProcessW failed with error code {err}")

    pid = pi.dwProcessId
    kernel32.CloseHandle(pi.hThread)
    kernel32.CloseHandle(pi.hProcess)
    logger.info("Spawned process PID %d on WinSta0\\Default", pid)
    return pid
