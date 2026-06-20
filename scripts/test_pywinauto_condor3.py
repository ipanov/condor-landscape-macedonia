"""
Test script: inspect Condor Load flight plan dialog.
"""
import time
import sys
from pywinauto import Application


def main():
    try:
        app = Application(backend="uia").connect(title="Load flight plan")
        dlg = app.window(title="Load flight plan")
        print("Connected to Load flight plan dialog.")
        print("Window rect:", dlg.rectangle())

        print("\n--- Controls ---")
        dlg.print_control_identifiers(filename=None, depth=3)
    except Exception as e:
        print("Error:", e)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
