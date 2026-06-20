import time
import os
import pywinauto
from pywinauto import Application, keyboard

RAW_PATH = os.path.abspath('D:/Repos/condor-landscape/sources/dem/macedonia_skopje_dem_utm30m.raw')
LANDSCAPE_NAME = 'MacedoniaSkopje'
CONDOR_LANDSCAPES = 'C:/Condor2/Landscapes'
OUT_DIR = os.path.join(CONDOR_LANDSCAPES, LANDSCAPE_NAME)
UTM_ZONE = '34'
HEMISPHERE = 'N'  # combo? We'll inspect
EASTING = '506880'
NORTHING = '4700160'


def wait_for(condition, timeout=30, interval=0.5):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if condition():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def set_edit_text(edit, text):
    """Set text of a UIA Edit control robustly."""
    edit.click_input()
    time.sleep(0.1)
    # Try set_text first
    try:
        edit.set_text(text)
    except Exception as e:
        print(f"set_text failed: {e}, using keyboard")
        keyboard.send_keys('{HOME}')
        keyboard.send_keys('+{END}')
        keyboard.send_keys('{DELETE}')
        keyboard.send_keys(text)
    # Verify
    actual = edit.window_text()
    if actual != text:
        print(f"Warning: edit text is '{actual}', expected '{text}'")


def main():
    print(f"RAW_PATH: {RAW_PATH}")
    print(f"OUT_DIR: {OUT_DIR}")

    # Ensure output directory exists
    os.makedirs(os.path.join(OUT_DIR, 'HeightMaps'), exist_ok=True)

    # Kill existing RawToTrn
    os.system('taskkill /F /IM RawToTrn.exe >nul 2>&1')
    time.sleep(0.5)

    app = Application(backend='uia').start('D:/Repos/condor-landscape/tools/CLT2.7/RawToTrn.exe')
    time.sleep(1.5)

    dlg = app.window(title='Condor Landscape Toolkit 2: RAW to TRN')
    dlg.set_focus()
    time.sleep(0.2)

    load_pane = dlg.child_window(title="Load", control_type="Pane")

    # Identify width and height edits by vertical position
    edits = load_pane.descendants(control_type="Edit")
    print(f"Found {len(edits)} edits")
    if len(edits) < 2:
        raise RuntimeError("Could not find width/height edits")
    width_edit = min(edits, key=lambda e: e.rectangle().top)
    height_edit = max(edits, key=lambda e: e.rectangle().top)

    set_edit_text(width_edit, '2304')
    set_edit_text(height_edit, '2304')

    # Check Flip vertical
    flip_vert = load_pane.child_window(title="Flip vertical", control_type="CheckBox")
    flip_vert.click_input()
    time.sleep(0.1)

    # Select 30 m resolution
    rb_30 = load_pane.child_window(title="30 m", control_type="RadioButton")
    rb_30.click_input()
    time.sleep(0.1)

    # Click Load raw heightmap
    load_btn = load_pane.child_window(title="Load raw heightmap", control_type="Button")
    load_btn.click_input()
    print("Clicked Load raw heightmap")

    # Wait for Open dialog
    if not wait_for(lambda: app.window(title_re='Open').exists(), timeout=10):
        raise RuntimeError("Open dialog did not appear")
    open_dlg = app.window(title_re='Open')
    open_dlg.set_focus()
    time.sleep(0.3)

    # Type path and confirm
    # Use the file name edit (first Edit in dialog)
    try:
        fname_edit = open_dlg.child_window(control_type="Edit", found_index=0)
        set_edit_text(fname_edit, RAW_PATH)
    except Exception as e:
        print(f"Could not set filename edit: {e}, falling back to typing")
        keyboard.send_keys(RAW_PATH)

    time.sleep(0.2)
    keyboard.send_keys('{ENTER}')
    print("Confirmed Open dialog")

    # Wait for dialog to close
    if not wait_for(lambda: not open_dlg.exists(), timeout=10):
        print("Warning: Open dialog still exists")

    # Wait for load to complete by checking progress bar value
    print("Waiting for load...")
    loaded = False
    for i in range(60):
        time.sleep(1)
        try:
            pb = load_pane.child_window(control_type="ProgressBar")
            props = pb.legacy_properties()
            val = props.get('Value', '')
            print(f"  Progress: {val}")
            if str(val) == '100':
                loaded = True
                break
        except Exception as e:
            print(f"  Progress check error: {e}")
            # If progress bar gone, assume loaded
            loaded = True
            break

    if not loaded:
        print("Warning: load may not have completed")

    time.sleep(1)
    img_path = 'D:/Repos/condor-landscape/tools/rawtotrn_after_load.png'
    dlg.capture_as_image().save(img_path)
    print(f"Screenshot after load: {img_path}")

    # Switch to Save tab
    tab = dlg.child_window(control_type="Tab")
    try:
        tab.select("Save")
        print("Selected Save tab via select")
    except Exception as e:
        print(f"Select Save failed: {e}")
        save_item = dlg.child_window(title="Save", control_type="TabItem")
        save_item.click_input()
        print("Clicked Save tab item")

    time.sleep(0.5)
    img_path = 'D:/Repos/condor-landscape/tools/rawtotrn_save_tab.png'
    dlg.capture_as_image().save(img_path)
    print(f"Screenshot Save tab: {img_path}")

    # Inspect Save pane controls
    save_panes = [p for p in dlg.descendants(control_type="Pane") if p.window_text() in ('Save', 'UTM zone')]
    print(f"Save-related panes: {len(save_panes)}")
    for p in save_panes:
        print(f"  Pane '{p.window_text()}' rect={p.rectangle()}")
        try:
            p.print_control_identifiers()
        except Exception as e:
            print(f"  print error: {e}")

    # Try to find and set Save controls
    # Look for group boxes or panes with 'UTM zone' and calibration edits
    all_edits = dlg.descendants(control_type="Edit")
    all_combos = dlg.descendants(control_type="ComboBox")
    all_radios = dlg.descendants(control_type="RadioButton")
    all_buttons = dlg.descendants(control_type="Button")

    print(f"All edits: {len(all_edits)}")
    print(f"All combos: {len(all_combos)}")
    print(f"All radios: {len(all_radios)}")
    print(f"All buttons: {len(all_buttons)}")

    for i, e in enumerate(all_edits):
        try:
            print(f"  Edit {i}: '{e.window_text()}' rect={e.rectangle()}")
        except Exception as ex:
            print(f"  Edit {i}: error {ex}")

    for i, c in enumerate(all_combos):
        try:
            print(f"  Combo {i}: '{c.window_text()}' rect={c.rectangle()}")
        except Exception as ex:
            print(f"  Combo {i}: error {ex}")

    for i, r in enumerate(all_radios):
        try:
            print(f"  Radio {i}: '{r.window_text()}' rect={r.rectangle()}")
        except Exception as ex:
            print(f"  Radio {i}: error {ex}")

    for i, b in enumerate(all_buttons):
        try:
            txt = b.window_text()
            if txt:
                print(f"  Button {i}: '{txt}' rect={b.rectangle()}")
        except Exception as ex:
            print(f"  Button {i}: error {ex}")

    print("Automation script finished. Keeping window open for inspection.")
    time.sleep(10)


if __name__ == '__main__':
    main()
