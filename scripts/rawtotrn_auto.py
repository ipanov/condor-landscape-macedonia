#!/usr/bin/env python3
"""Drive official RawToTrn.exe end-to-end via pywinauto (elevated-capable).
Auto-detects/dismisses popups. Produces correctly-oriented .trn + .tr3.
Phase 1 (this run): Load DEM, switch to Save tab, dump + screenshot it.
"""
import time, os, sys, shutil, win32gui
from pywinauto import Application, keyboard, mouse

EXE = 'D:/Repos/condor-landscape/tools/CLT2.7/RawToTrn.exe'
RAW = os.path.abspath('D:/Repos/condor-landscape/sources/dem/macedonia_skopje_dem_utm30m_flat.raw')
TITLE = 'Condor Landscape Toolkit 2: RAW to TRN'
SHOT = 'D:/Repos/condor-landscape/.sandbox/rawtotrn_savetab.png'
OUT_TRN = r'C:\Condor2\Landscapes\MacedoniaSkopje\MacedoniaSkopje.trn'
OUT_DIR = r'C:\Condor2\Landscapes\MacedoniaSkopje\HeightMaps'
EAST, NORTH, ZONE = '506880', '4700160', '34'

def p(*a): print(*a, flush=True)

def popups():
    res = []
    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) in ('#32770', 'TMessageForm'):
            res.append(h)
    win32gui.EnumWindows(cb, None); return res

def dismiss(app, accept=True):
    for h in popups():
        try:
            d = Application(backend='win32').connect(handle=h).window(handle=h)
            txt = ' | '.join(c.window_text() for c in d.descendants() if c.window_text())
            p("  POPUP:", txt)
            for b in (('OK', '&OK', 'Yes', '&Yes') if accept else ('No', '&No', 'Cancel')):
                try:
                    d.child_window(title=b, class_name='Button').click(); p("   ->", b); break
                except Exception: pass
        except Exception as e:
            p("  popup err", e)

os.system('taskkill /F /IM RawToTrn.exe >nul 2>&1'); time.sleep(1.2)
app = Application(backend='win32').start(EXE); time.sleep(2.0)
dlg = app.window(title=TITLE); dlg.set_focus()

lp = dlg.child_window(title='Load parameters', class_name='TGroupBox')
es = sorted(lp.descendants(class_name='TEdit'), key=lambda c: c.rectangle().top)
es[0].set_text('2304'); es[1].set_text('2304')
fv = dlg.child_window(title='Flip vertical', class_name='TCheckBox')
if fv.get_check_state() == 0: fv.click()
dlg.child_window(title='30 m', class_name='TRadioButton').click()
p("Load: w/h=", es[0].window_text(), es[1].window_text(), "flipV=", fv.get_check_state())

dlg.child_window(title='Load raw heightmap', class_name='TButton').click(); time.sleep(1.5)
od = app.window(title='Open'); od.set_focus()
od.child_window(class_name='Edit', found_index=0).set_text(RAW); time.sleep(0.3)
keyboard.send_keys('{ENTER}'); p("raw loading..."); time.sleep(5)
dismiss(app); time.sleep(0.5)

# Backup existing outputs
if os.path.exists(OUT_TRN):
    shutil.move(OUT_TRN, OUT_TRN + '.bak')
if os.path.exists(OUT_DIR):
    bak = OUT_DIR + '_bak'
    if os.path.exists(bak): shutil.rmtree(bak)
    shutil.copytree(OUT_DIR, bak)

# Switch to Save tab using Ctrl+Tab from Load tab
for _ in range(3):
    dlg.set_focus()
    keyboard.send_keys('^{TAB}')
    time.sleep(0.6)
    # verify Save tab active by looking for UTM zone group
    try:
        test = dlg.child_window(title='UTM zone', class_name='TGroupBox')
        _ = test.rectangle()
        p("Save tab active")
        break
    except Exception:
        p("Save tab not active yet, retrying...")
dismiss(app)

try:
    dlg.capture_as_image().save(SHOT); p("screenshot ->", SHOT)
except Exception as e:
    p("capture err", e)

p("=== ALL CONTROLS (Save tab) ===")
for w in dlg.descendants():
    try:
        rr = w.rectangle()
        p(f"  {w.friendly_class_name():13s} {w.window_text()!r:24s} ({rr.left},{rr.top})")
    except Exception:
        pass

# Set calibration by clicking Save tab controls directly
dlg.set_focus(); time.sleep(0.2)
# Set UTM zone combos (zone + hemisphere)
zone_gb = dlg.child_window(title='UTM zone', class_name='TGroupBox')
combos = sorted(zone_gb.descendants(class_name='TComboBox'), key=lambda c: c.rectangle().left)
if len(combos) >= 2:
    # Left combo likely zone number, right likely hemisphere
    try:
        combos[0].select('34')
        combos[1].select('N')
        p(f"UTM zone set: {combos[0].window_text()}, {combos[1].window_text()}")
    except Exception as e:
        p("UTM combo set error:", e)

# Select Top left corner
for w in dlg.descendants():
    try:
        if w.friendly_class_name() == 'RadioButton' and w.window_text() == 'Top left':
            if w.get_check_state() == 0: w.click()
            p("Top left selected")
            break
    except Exception: pass

# Set easting/northing in Corner coordinates group
cc = dlg.child_window(title='Corner coordinates', class_name='TGroupBox')
edits = sorted(cc.descendants(class_name='TEdit'), key=lambda c: c.rectangle().top)
if len(edits) >= 2:
    edits[0].set_text(EAST)
    edits[1].set_text(NORTH)
    p(f"Easting/Northing set: {edits[0].window_text()}, {edits[1].window_text()}")
else:
    p(f"ERROR: only {len(edits)} corner edits found")

# Click Save to TRN
for w in dlg.descendants(class_name='TButton'):
    if 'save' in w.window_text().lower() and 'trn' in w.window_text().lower():
        w.click()
        p("Save to TRN clicked")
        break

# Wait for completion
for _ in range(60):
    dismiss(app)
    if os.path.exists(OUT_TRN) and os.path.getsize(OUT_TRN) > 1000:
        p(f"SUCCESS .trn size={os.path.getsize(OUT_TRN)}")
        break
    time.sleep(0.5)
else:
    p("Save did not complete in 30s")
p("DONE")
