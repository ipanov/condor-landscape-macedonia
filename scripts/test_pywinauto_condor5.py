"""
Test script: open Load dialog and inspect User flightplans list.
"""
import time
import sys
from pywinauto import Application


def wait_for_window(app, title, timeout=10):
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
        app = Application(backend="uia").connect(title="FLIGHT PLANNER")
        fp = app.window(title="FLIGHT PLANNER")
        print("Connected to Flight Planner.")

        # Click Load
        load_btn = fp.child_window(title="Load", control_type="Pane")
        load_btn.click_input()
        print("Clicked Load.")

        # Wait for dialog
        dlg = wait_for_window(app, "Load flight plan")
        print("Load dialog open.")
        time.sleep(1)

        # Click User flightplans tab
        user_tab = dlg.child_window(title="User flightplans", control_type="TabItem")
        if user_tab.exists():
            print("Clicking User flightplans...")
            user_tab.click_input()
            time.sleep(1)

        # Print all descendants
        print("\n--- All descendants ---")
        for ctrl in dlg.descendants():
            try:
                txt = ctrl.window_text()
                ctype = ctrl.element_info.control_type
                rect = ctrl.rectangle()
                print(f"  {ctype}: '{txt}' rect={rect}")
            except Exception as e:
                print(f"  error: {e}")

        # Try list pane
        list_pane = dlg.child_window(auto_id="531074", control_type="Pane")
        print("\nList pane found:", list_pane.exists())
        if list_pane.exists():
            print("List pane rect:", list_pane.rectangle())
            print("Children:")
            for c in list_pane.children():
                print(f"  {c.element_info.control_type}: '{c.window_text()}' rect={c.rectangle()}")
            print("Descendants:")
            for c in list_pane.descendants():
                print(f"  {c.element_info.control_type}: '{c.window_text()}' rect={c.rectangle()}")

        print("\nDone. Dialog remains open for manual inspection.")
        time.sleep(10)

    except Exception as e:
        print("Error:", e)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
