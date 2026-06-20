#!/usr/bin/env python3
"""Monitor WMS health and resume orthophoto downloads when server recovers."""
import os
import sys
import time
import subprocess
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "scripts" / "download_all_quadrants.py"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TEST_URL = (
    "https://e-uslugi.katastar.gov.mk/geo/proxy/gwc/wms?"
    "SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS=APP_DATA:ORTOFOTO_2023&STYLES=&"
    "FORMAT=image/jpeg&TILED=true&RESIZE=resize&GRIDSET=MSCS6316&SRS=EPSG:6316&"
    "BBOX=7398000.634424793,4521901.793180252,7398056.634424793,4521957.793180252&"
    "WIDTH=256&HEIGHT=256"
)
HEADERS = {
    "Referer": "https://e-uslugi.katastar.gov.mk/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def wms_healthy():
    try:
        req = urllib.request.Request(TEST_URL, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            return r.status == 200 and len(data) > 1000 and r.headers.get("Content-Type", "").startswith("image")
    except Exception as e:
        return False


def main():
    concurrency = int(sys.argv[1]) if len(sys.argv) > 1 else 64
    print("WMS monitor started. Testing every 60 seconds...", flush=True)
    while True:
        if wms_healthy():
            print(f"{time.strftime('%H:%M:%S')} WMS is healthy. Starting downloads...", flush=True)
            logf = open(LOG_DIR / "download_auto_resume.log", "a")
            proc = subprocess.Popen(
                [sys.executable, str(WRAPPER), str(concurrency)],
                stdout=logf, stderr=subprocess.STDOUT, text=True
            )
            proc.wait()
            logf.close()
            print(f"{time.strftime('%H:%M:%S')} Download process exited (code {proc.returncode}). Will recheck server.", flush=True)
        else:
            print(f"{time.strftime('%H:%M:%S')} WMS unhealthy or timed out. Waiting...", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
