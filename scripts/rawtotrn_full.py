#!/usr/bin/env python3
"""End-to-end RawToTrn driver (pywinauto, elevated-capable, popup-aware).
Load 2304x2304 DEM (Flip vertical, 30m) -> Save tab calibration (Top-left
E=506880 N=4700160, UTM 34 N) -> Save to TRN -> handle Save-As + popups.
Writes correctly-oriented MacedoniaSkopje.trn + 144 .tr3."""
import time, os, struct, glob, win32gui
from pywinauto import Application, keyboard, mouse

EXE = 'D:/Repos/condor-landscape/tools/CLT2.7/RawToTrn.exe'
RAW = os.path.abspath('D:/Repos/condor-landscape/sources/dem/macedonia_skopje_dem_utm30m_flat.raw')
TITLE = 'Condor Landscape Toolkit 2: RAW to TRN'
OUT = r'C:\Condor2\Landscapes\MacedoniaSkopje\MacedoniaSkopje.trn'
HM = r'C:\Condor2\Landscapes\MacedoniaSkopje\HeightMaps'
EAST, NORTH = '506880', '4700160'

def p(*a): print(*a, flush=True)

def dlgs():
    res = []
    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) in ('#32770', 'TMessageForm'):
            res.append((h, win32gui.GetClassName(h)))
    win32gui.EnumWindows(cb, None); return res

def click_popups(buttons):
    done = []
    for h, cls in dlgs():
        d = Application(backend='win32').connect(handle=h).window(handle=h)
        t = ' | '.join(c.window_text() for c in d.descendants() if c.window_text())
        if any('File' in x for x in t.split('|')):  # don't treat Save-As as a popup
            continue
        for b in buttons:
            try:
                d.child_window(title=b, class_name='Button').click(); done.append((t[:45], b)); break
            except Exception: pass
    return done

os.system('taskkill /F /IM RawToTrn.exe >nul 2>&1'); time.sleep(1.2)
app = Application(backend='win32').start(EXE); time.sleep(2.0)
dlg = app.window(title=TITLE); dlg.set_focus()

lp = dlg.child_window(title='Load parameters', class_name='TGroupBox')
es = sorted(lp.descendants(class_name='TEdit'), key=lambda c: c.rectangle().top)
es[0].set_text('2304'); es[1].set_text('2304')
fv = dlg.child_window(title='Flip vertical', class_name='TCheckBox')
if fv.get_check_state() == 0: fv.click()
dlg.child_window(title='30 m', class_name='TRadioButton').click()
dlg.child_window(title='Load raw heightmap', class_name='TButton').click(); time.sleep(1.5)
od = app.window(title='Open'); od.set_focus()
od.child_window(class_name='Edit', found_index=0).set_text(RAW); time.sleep(0.3)
keyboard.send_keys('{ENTER}'); time.sleep(5)
p("loaded; popup:", click_popups(['OK', '&OK'])); time.sleep(0.5)

r = win32gui.GetWindowRect(dlg.handle)
mouse.click(coords=(r[0] + 178, r[1] + 58)); time.sleep(0.9)   # Save tab
click_popups(['OK', 'Yes'])

dlg.child_window(title='Top left', class_name='TRadioButton').click()
ccg = dlg.child_window(title='Corner coordinates', class_name='TGroupBox')
ce = sorted(ccg.descendants(class_name='TEdit'), key=lambda e: e.rectangle().top)
ce[0].set_text(EAST); ce[1].set_text(NORTH)
for c in sorted(dlg.child_window(title='UTM zone', class_name='TGroupBox').descendants(class_name='TComboBox'),
                key=lambda c: c.rectangle().left):
    items = c.item_texts()
    if '34' in items: c.select('34')
    elif 'N' in items: c.select('N')
    elif 'North' in items: c.select('North')
p("calib:", ce[0].window_text(), ce[1].window_text())

dlg.child_window(title='Save to TRN', class_name='TButton').click(); time.sleep(1.6)
# Save-As dialog
for h, cls in dlgs():
    d = Application(backend='win32').connect(handle=h).window(handle=h)
    texts = [c.window_text() for c in d.descendants() if c.window_text()]
    if any('File' in t for t in texts):
        d.set_focus()
        try:
            d.child_window(class_name='ComboBoxEx32').child_window(class_name='Edit').set_text(OUT)
        except Exception:
            d.child_window(class_name='Edit', found_index=0).set_text(OUT)
        time.sleep(0.3)
        for b in ('&Save', 'Save'):
            try:
                d.child_window(title=b, class_name='Button').click(); p("clicked", b); break
            except Exception: pass
        break
time.sleep(1.5)
for _ in range(6):
    got = click_popups(['&Yes', 'Yes', 'OK', '&OK'])
    if got: p("popup:", got)
    time.sleep(1.2)

ok = os.path.exists(OUT)
p("TRN:", ok, (os.path.getsize(OUT) if ok else 0), "| TR3:", len(glob.glob(HM + r'\*.tr3')))
if ok:
    hh = open(OUT, 'rb').read(36)
    w, ht = struct.unpack('<ii', hh[:8]); px = struct.unpack('<fff', hh[8:20])
    e, n = struct.unpack('<ff', hh[20:28]); z, _, hemi, _ = struct.unpack('<HHHH', hh[28:36])
    p(f"TRN header: {w}x{ht} px={px} E={e:.0f} N={n:.0f} zone={z} hemi={hemi}")
