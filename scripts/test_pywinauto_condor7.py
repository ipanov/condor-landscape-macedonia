"""
Inspect the User flightplans list pane.
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
        if user_tab.exists():
            user_tab.click_input()
            print("Clicked User flightplans.")
            time.sleep(1)

        # Inspect list pane by relative position near "Flightplan name:"
        list_pane = dlg.child_window(auto_id="596438", control_type="Pane")
        print("List pane exists:", list_pane.exists())
        if list_pane.exists():
            print("List pane rect:", list_pane.rectangle())
            print("Children:")
            for c in list_pane.children():
                print(f"  {c.element_info.control_type}: '{c.window_text()}' rect={c.rectangle()}")
            print("Descendants:")
            for c in list_pane.descendants():
                print(f"  {c.element_info.control_type}: '{c.window_text()}' rect={c.rectangle()}")

        # Try to find any element with 'MacedoniaSkopje' in name
        print("\n--- Searching for MacedoniaSkopje ---")
        for ctrl in dlg.descendants():
            try:
                txt = ctrl.window_text()
                if "Macedonia" in txt or "Skopje" in txt or "Test" in txt:
                    print(f"FOUND {ctrl.element_info.control_type}: '{txt}' rect={ctrl.rectangle()}")
            except Exception:
                pass

        # Take a screenshot of the dialog using pywinauto
        print("\nSaving dialog screenshot...")
        img_path = "D:/Repos/condor-landscape/scripts/load_dialog_screenshot.png"
        dlg.capture_as_image().save(img_path)
        print("Saved to", img_path)

        time.sleep(5)

    except Exception as e:
        print("Error:", e)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
