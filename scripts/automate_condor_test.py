#!/usr/bin/env python3
"""Attempt to automate Condor 2 menu navigation with low-level mouse input.

This script must be run with administrator privileges so it can send input to
Condor's elevated TGUIForm window. If run non-elevated, the mouse/keyboard
sends will be ignored by Windows UIPI.
"""
import subprocess
import sys
import time
from pathlib import Path

# Optional win32 imports; fall back to ctypes if not installed.
try:
    import win32api
    import win32con
    import win32gui
    HAS_WIN32 = True
except Exception:
    HAS_WIN32 = False
    import ctypes
    from ctypes import wintypes

CONDOR_EXE = Path("C:/Condor2/Condor.exe")
MAIN_MENU_TITLE = "Condor version 2.2.0"
FLIGHT_PLANNER_TITLE = "FLIGHT PLANNER"
WINDOW_CLASS = "TGUIForm"

# Estimated relative button positions inside the Condor main menu.
# These are relative to the window client area origin (from screenshots).
BTN_FREE_FLIGHT = (155, 198)  # main menu left button column
BTN_START = (380, 500)        # flight planner bottom-right "Start flight"


def log(msg):
    print(msg, flush=True)


def find_window(title):
    if HAS_WIN32:
        return win32gui.FindWindow(WINDOW_CLASS, title)
    else:
        user32 = ctypes.windll.user32
        return user32.FindWindowW(WINDOW_CLASS, title)


def find_condor_window():
    return find_window(MAIN_MENU_TITLE)


def get_window_rect(hwnd):
    if HAS_WIN32:
        return win32gui.GetWindowRect(hwnd)
    else:
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return (rect.left, rect.top, rect.right, rect.bottom)


def click_at(x, y):
    log(f"Clicking at screen ({x}, {y})")
    if HAS_WIN32:
        win32api.SetCursorPos((x, y))
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    else:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(x, y)
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP


def click_relative(hwnd, rel_x, rel_y):
    left, top, right, bottom = get_window_rect(hwnd)
    x = left + rel_x
    y = top + rel_y
    click_at(x, y)


def main():
    log("Starting Condor automation test")
    if not CONDOR_EXE.exists():
        log(f"ERROR: Condor not found at {CONDOR_EXE}")
        sys.exit(1)

    # Launch Condor
    log("Launching Condor...")
    proc = subprocess.Popen([str(CONDOR_EXE)], cwd=str(CONDOR_EXE.parent))

    # Wait for window
    hwnd = 0
    for i in range(30):
        time.sleep(1)
        hwnd = find_condor_window()
        if hwnd:
            log(f"Found Condor window handle {hwnd}")
            break
        log(f"Waiting for Condor window... {i+1}s")
    if not hwnd:
        log("ERROR: Condor window not found")
        proc.terminate()
        sys.exit(1)

    # Give menu time to render
    time.sleep(2)

    # Click FREE FLIGHT
    log("Clicking FREE FLIGHT...")
    click_relative(hwnd, *BTN_FREE_FLIGHT)
    time.sleep(3)

    # Wait for flight planner window and click Start
    log("Waiting for Flight Planner window...")
    fpl_hwnd = 0
    for i in range(10):
        time.sleep(1)
        fpl_hwnd = find_window(FLIGHT_PLANNER_TITLE)
        if fpl_hwnd:
            log(f"Found Flight Planner window handle {fpl_hwnd}")
            break
    if not fpl_hwnd:
        log("WARNING: Flight Planner window not found; using main window handle")
        fpl_hwnd = hwnd

    log("Clicking Start flight...")
    click_relative(fpl_hwnd, *BTN_START)
    time.sleep(15)

    log("Automation sequence complete. Check Condor window for 3D world or crash.")


if __name__ == "__main__":
    main()
