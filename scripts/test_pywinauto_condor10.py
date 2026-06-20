"""
Inspect current Load dialog state and try clicking OK.
"""
import time
from pywinauto import Application

app = Application(backend="uia").connect(title="Load flight plan")
dlg = app.window(title="Load flight plan")
print("Dialog rect:", dlg.rectangle())

# Print all descendants
print("\n--- Descendants ---")
for ctrl in dlg.descendants():
    try:
        print(f"  {ctrl.element_info.control_type}: '{ctrl.window_text()}' rect={ctrl.rectangle()}")
    except Exception as e:
        print(f"  error: {e}")

# Click OK
ok = dlg.child_window(title="OK", control_type="Pane")
print("\nOK rect:", ok.rectangle())
print("Clicking OK...")
ok.click_input()
time.sleep(2)
print("Dialog exists after OK click:", dlg.exists())

# Save screenshot
dlg.capture_as_image().save("D:/Repos/condor-landscape/scripts/load_dialog_state.png")
print("Screenshot saved.")
