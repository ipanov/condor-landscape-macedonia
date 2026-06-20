"""
Test keyboard navigation in Load flight plan dialog.
"""
import time
import sys
from pywinauto import Application, keyboard


def main():
    try:
        app = Application(backend="uia").connect(title="Load flight plan")
        dlg = app.window(title="Load flight plan")
        print("Connected to Load flight plan dialog.")

        # Make sure User flightplans is selected
        user_tab = dlg.child_window(title="User flightplans", control_type="TabItem")
        if user_tab.exists():
            user_tab.click_input()
            time.sleep(0.5)

        # Focus list by clicking in it
        list_pane = dlg.child_window(auto_id="596438", control_type="Pane")
        list_ctrl = list_pane.child_window(control_type="List")
        print("List ctrl exists:", list_ctrl.exists())
        if list_ctrl.exists():
            list_ctrl.click_input()
            print("Clicked list.")
            time.sleep(0.5)

            # Send Home to go to top
            keyboard.send_keys('{HOME}')
            time.sleep(0.5)
            # Type M to jump to MacedoniaSkopje
            keyboard.send_keys('M')
            time.sleep(0.5)

        # Read details edit
        details_edit = dlg.child_window(auto_id="1317418", control_type="Edit")
        print("Details edit text after M:", repr(details_edit.window_text()))

        # Try arrow down a few times
        for i in range(5):
            keyboard.send_keys('{DOWN}')
            time.sleep(0.3)
            txt = details_edit.window_text()
            print(f"After DOWN {i+1}:", repr(txt[:200]))
            if "MacedoniaSkopje" in txt or "Skopje" in txt:
                print("Found target flight plan!")
                break

        time.sleep(3)

    except Exception as e:
        print("Error:", e)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
