#!/usr/bin/env python3
"""Monitor MK cadastre WMS and auto-resume downloads when it recovers."""
import time, subprocess, urllib.request, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_URL = ("https://e-uslugi.katastar.gov.mk/geo/proxy/gwc/wms?"
    "SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS=APP_DATA:ORTOFOTO_2023"
    "&FORMAT=image/jpeg&TILED=true&RESIZE=resize&GRIDSET=MSCS6316&SRS=EPSG:6316"
    "&BBOX=7540522.558,4660405.273,7540594.238,4660476.953&WIDTH=256&HEIGHT=256")
HEADERS = {"Referer": "https://e-uslugi.katastar.gov.mk/",
           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

def check_wms():
    try:
        req = urllib.request.Request(TEST_URL, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            ct = resp.headers.get("Content-Type", "")
            if ct.startswith("image") and len(data) > 500:
                return True
    except:
        pass
    return False

def main():
    interval = 120  # check every 2 minutes
    print(f"WMS monitor started. Checking every {interval}s...", flush=True)
    attempt = 0
    while True:
        attempt += 1
        ok = check_wms()
        ts = time.strftime("%H:%M:%S")
        if ok:
            print(f"[{ts}] WMS IS BACK! Starting downloads...", flush=True)
            # Rebuild cache index first
            subprocess.run([sys.executable, str(ROOT / "scripts/build_cache_index.py")],
                          cwd=str(ROOT))
            # Launch parallel downloads
            subprocess.run([sys.executable, str(ROOT / "scripts/download_all_quadrants.py"), "128"],
                          cwd=str(ROOT))
            print(f"[{ts}] Downloads complete or failed. Re-monitoring...", flush=True)
        else:
            if attempt % 5 == 1:  # print every 5th check
                print(f"[{ts}] WMS still down (attempt {attempt})", flush=True)
        time.sleep(interval)

if __name__ == "__main__":
    main()
