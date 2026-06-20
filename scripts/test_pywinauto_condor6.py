"""
Full sequence test: Condor main menu -> Free Flight -> Load -> inspect dialog.
"""
import time
import sys
from pywinauto import Application


def wait_for_window(app, title, timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        try:
            w = app.window(title=title)
            if w.exists():
                return w
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Window '{title}' did not appear")


def main():
    try:
        # Connect to main Condor window
        app = Application(backend="uia").connect(title="Condor version 2.2.0")
        main_dlg = app.window(title="Condor version 2.2.0")
        print("Connected to main Condor window.")

        # Click FREE FLIGHT
        ff = main_dlg.child_window(title="FREE FLIGHT", control_type="Pane")
        print("FREE FLIGHT exists:", ff.exists(), "rect:", ff.rectangle())
        ff.click_input()
        print("Clicked FREE FLIGHT.")

        # Wait for Flight Planner
        fp = wait_for_window(app, "FLIGHT PLANNER")
        print("Flight Planner open.")
        time.sleep(1)

        # Click Load
        load_btn = fp.child_window(title="Load", control_type="Pane")
        load_btn.click_input()
        print("Clicked Load.")

        # Wait for Load dialog
        dlg = wait_for_window(app, "Load flight plan")
        print("Load dialog open.")
        time.sleep(1)

        # Click User flightplans tab
        user_tab = dlg.child_window(title="User flightplans", control_type="TabItem")
        if user_tab.exists():
            user_tab.click_input()
            print("Clicked User flightplans.")
            time.sleep(1)

        # Inspect list area
        list_pane = dlg.child_window(auto_id="531074", control_type="Pane")
        print("List pane exists:", list_pane.exists())
        if list_pane.exists():
            print("List pane rect:", list_pane.rectangle())
            for c in list_pane.descendants():
                print(f"  {c.element_info.control_type}: '{c.window_text()}' rect={c.rectangle()}")

        # Inspect details edit
        details_edit = dlg.child_window(auto_id="465620", control_type="Edit")
        print("Details edit exists:", details_edit.exists())
        if details_edit.exists():
            print("Details edit text:", details_edit.window_text())

        print("\nDone. Dialog remains open.")
        time.sleep(10)

    except Exception as e:
        print("Error:", e)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
