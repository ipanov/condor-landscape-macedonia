"""
Test script: use pywinauto to interact with Condor 2 Flight Planner.
Run as admin if Condor is elevated.
"""
import time
import sys
from pywinauto import Application

CONDOR_TITLE = "FLIGHT PLANNER"


def main():
    print("Looking for Condor Flight Planner window...")
    try:
        app = Application(backend="uia").connect(title=CONDOR_TITLE)
        dlg = app.window(title=CONDOR_TITLE)
        print("Connected to Flight Planner.")
        print("Window rect:", dlg.rectangle())

        # Print all descendants for inspection
        print("\n--- Controls ---")
        dlg.print_control_identifiers(filename=None, depth=2)

        # Find Load pane
        load_btn = dlg.child_window(title="Load", control_type="Pane")
        print("\nLoad found:", load_btn.exists())
        if load_btn.exists():
            print("Load rect:", load_btn.rectangle())
            print("Clicking Load...")
            load_btn.click_input()
            print("Click sent.")
            time.sleep(2)

            # Look for dialog
            print("\nLooking for dialog windows...")
            for w in app.windows():
                print("  Window:", w.window_text(), "class:", w.class_name())
        else:
            print("Could not find Load element.")
    except Exception as e:
        print("Error:", e)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
