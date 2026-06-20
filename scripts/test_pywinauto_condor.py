"""
Test script: use pywinauto to interact with Condor 2.
Run as admin if Condor is elevated.
"""
import time
import sys
from pywinauto import Application, Desktop

CONDOR_TITLE = "Condor version 2.2.0"


def main():
    print("Looking for Condor window...")
    try:
        app = Application(backend="uia").connect(title=CONDOR_TITLE)
        dlg = app.window(title=CONDOR_TITLE)
        print("Connected to Condor window.")
        print("Window rect:", dlg.rectangle())
        print("Is visible:", dlg.is_visible())
        print("Is enabled:", dlg.is_enabled())

        # Try to find Free Flight pane
        free_flight = dlg.child_window(title="FREE FLIGHT", control_type="Pane")
        print("Free Flight found:", free_flight.exists())
        if free_flight.exists():
            print("Free Flight rect:", free_flight.rectangle())
            print("Clicking Free Flight via click_input...")
            free_flight.click_input()
            print("Click sent.")
            time.sleep(2)
            print("Done.")
        else:
            print("Could not find FREE FLIGHT element.")
    except Exception as e:
        print("Error:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
