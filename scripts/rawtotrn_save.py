#!/usr/bin/env python3
"""Complete RawToTrn Save tab -> write correctly-oriented .trn + .tr3.
Connects to the already-open RawToTrn (DEM loaded, Save tab). Sets calibration
(Top-left corner E=506880 N=4700160, UTM 34 N), clicks Save to TRN, handles the
save dialog + overwrite + completion popups, then verifies the output."""
import time, os, struct, glob, win32gui
from pywinauto import Application, keyboard

TITLE = 'Condor Landscape Toolkit 2: RAW to TRN'
OUT_TRN = r'C:\Condor2\Landscapes\MacedoniaSkopje\MacedoniaSkopje.trn'
HM = r'C:\Condor2\Landscapes\MacedoniaSkopje\HeightMaps'
EAST, NORTH = '506880', '4700160'

def p(*a): print(*a, flush=True)

def pops():
    res = []
    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) in ('#32770', 'TMessageForm'):
            res.append(h)
    win32gui.EnumWindows(cb, None); return res

def dismiss(accept=True):
    msgs = []
    for h in pops():
        d = Application(backend='win32').connect(handle=h).window(handle=h)
        msgs.append(' | '.join(c.window_text() for c in d.descendants() if c.window_text()))
        for b in (('OK', '&OK', 'Yes', '&Yes') if accept else ('No', '&No')):
            try:
                d.child_window(title=b, class_name='Button').click(); break
            except Exception: pass
    return msgs

app = Application(backend='win32').connect(title=TITLE, timeout=8)
dlg = app.window(title=TITLE); dlg.set_focus(); time.sleep(0.3)

dlg.child_window(title='Top left', class_name='TRadioButton').click()

ccg = dlg.child_window(title='Corner coordinates', class_name='TGroupBox')
ce = sorted(ccg.descendants(class_name='TEdit'), key=lambda e: e.rectangle().top)
ce[0].set_text(EAST); ce[1].set_text(NORTH)
p("corner coords (top,bottom):", ce[0].window_text(), ce[1].window_text())

combos = sorted(dlg.child_window(title='UTM zone', class_name='TGroupBox').descendants(class_name='TComboBox'),
                key=lambda c: c.rectangle().left)
for c in combos:
    try:
        items = c.item_texts()
        if '34' in items: c.select('34'); p("zone=34")
        else:
            for hv in ('N', 'North'):
                if hv in items: c.select(hv); p("hemi=", hv); break
    except Exception as e:
        p("combo err", e)

dlg.child_window(title='Save to TRN', class_name='TButton').click(); time.sleep(1.3)
p("after Save to TRN popups:", dismiss(True))
sd = app.window(title_re='Save.*'); sd.set_focus()
sd.child_window(class_name='Edit', found_index=0).set_text(OUT_TRN); time.sleep(0.3)
keyboard.send_keys('{ENTER}'); time.sleep(1.2)
p("save-dialog popups:", dismiss(True))   # overwrite confirm
time.sleep(4)
p("post-write popups:", dismiss(True))     # 'done' info
time.sleep(2)

ok = os.path.exists(OUT_TRN)
p("TRN exists:", ok, (os.path.getsize(OUT_TRN) if ok else 0))
p("TR3 count:", len(glob.glob(HM + r'\*.tr3')))
if ok:
    h = open(OUT_TRN, 'rb').read(36)
    w, ht = struct.unpack('<ii', h[:8]); px = struct.unpack('<fff', h[8:20])
    e, n = struct.unpack('<ff', h[20:28]); z, _, hemi, _ = struct.unpack('<HHHH', h[28:36])
    p(f"TRN header: {w}x{ht} px={px} E={e} N={n} zone={z} hemi={hemi}")
