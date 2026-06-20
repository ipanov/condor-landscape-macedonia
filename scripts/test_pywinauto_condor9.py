"""
Simple test: click FREE FLIGHT and monitor Condor windows.
"""
import time
from pywinauto import Application

app = Application(backend="uia").connect(title="Condor version 2.2.0")
main = app.window(title="Condor version 2.2.0")
print("Main window:", main.rectangle())
main.set_focus()
time.sleep(2)

ff = main.child_window(title="FREE FLIGHT", control_type="Pane")
print("FREE FLIGHT rect:", ff.rectangle())
print("Clicking...")
ff.click_input()

for i in range(20):
    time.sleep(1)
    print(f"\n--- second {i+1} ---")
    for w in app.windows():
        try:
            print(f"  '{w.window_text()}' class={w.class_name()} rect={w.rectangle()} visible={w.is_visible()}")
        except Exception as e:
            print(f"  error: {e}")

    # Try to capture Flight Planner if exists
    try:
        fp = app.window(title="FLIGHT PLANNER")
        if fp.exists():
            fp.capture_as_image().save(f"D:/Repos/condor-landscape/scripts/fp_check_{i+1}.png")
            print(f"  Captured fp_check_{i+1}.png")
    except Exception as e:
        print(f"  FP capture error: {e}")
