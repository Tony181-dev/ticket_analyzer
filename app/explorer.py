"""Open a folder in Windows Explorer and make sure its window is actually visible.

A window launched by a background process is often left behind the browser, minimized, or placed
at coordinates of a monitor that no longer exists. After opening it we find the window, restore it,
move it onto a screen when needed and bring it to the front.
"""
import ctypes
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path


def open_folder(path):
    os.startfile(str(path))  # noqa: S606 - local desktop app
    threading.Thread(target=_raise_window, args=(Path(path),), daemon=True).start()


def _raise_window(path):
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    for fn in (user32.IsIconic, user32.ShowWindow, user32.BringWindowToTop, user32.SetForegroundWindow):
        fn.argtypes = [wintypes.HWND] + ([ctypes.c_int] if fn is user32.ShowWindow else [])

    wanted = {path.name.lower(), str(path).lower().rstrip("\\")}
    found = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        cls, title = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value == "CabinetWClass":
            user32.GetWindowTextW(hwnd, title, 512)
            if title.value.lower().rstrip("\\") in wanted:
                found.append(hwnd)
        return True

    for _ in range(25):  # Explorer needs a moment to create the window
        time.sleep(0.2)
        found.clear()
        user32.EnumWindows(enum_proc(visit), 0)
        if found:
            break
    if not found:
        return
    hwnd = found[0]

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.2)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    if not user32.MonitorFromWindow(hwnd, 0):  # MONITOR_DEFAULTTONULL: the window is on no screen
        screen_w, screen_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        w = min(max(rect.right - rect.left, 600), screen_w - 160)
        h = min(max(rect.bottom - rect.top, 400), screen_h - 160)
        user32.SetWindowPos(hwnd, 0, 80, 80, w, h, 0x0040)  # SWP_SHOWWINDOW

    # Windows only lets the foreground thread's input queue take focus: attach to it briefly.
    me = kernel32.GetCurrentThreadId()
    fg = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    user32.AttachThreadInput(me, fg, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(me, fg, False)
