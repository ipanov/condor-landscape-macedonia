"""
Test script: select User flightplans tab and inspect list.
"""
import time
import sys
from pywinauto import Application


def main():
    try:
        app = Application(backend="uia").connect(title="Load flight plan")
        dlg = app.window(title="Load flight plan")
        print("Connected to Load flight plan dialog.")

        # Click User flightplans tab
        user_tab = dlg.child_window(title="User flightplans", control_type="TabItem")
        print("User flightplans found:", user_tab.exists())
        if user_tab.exists():
            print("Clicking User flightplans...")
            user_tab.click_input()
            time.sleep(1)

        # Try to find list items
        print("\n--- All descendants ---")
        for ctrl in dlg.descendants():
            try:
                txt = ctrl.window_text()
                ctype = ctrl.element_info.control_type
                rect = ctrl.rectangle()
                print(f"  {ctype}: '{txt}' rect={rect}")
            except Exception as e:
                print(f"  error: {e}")

        # Try to find the flightplan name pane and list items
        list_pane = dlg.child_window(auto_id="531074", control_type="Pane")
        print("\nList pane found:", list_pane.exists())
        if list_pane.exists():
            print("List pane rect:", list_pane.rectangle())
            print("List pane children:")
            for c in list_pane.children():
                print(f"  {c.element_info.control_type}: '{c.window_text()}' rect={c.rectangle()}")

    except Exception as e:
        print("Error:", e)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
