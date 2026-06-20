#!/usr/bin/env python3
"""Pre-build a shared cache index of already-downloaded orthophoto tiles."""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / ".sandbox" / "textures_mk2023_z11"
INDEX_PATH = OUT_DIR / "cache_index.txt"
PATTERN = re.compile(r"z11_x(-?\d+)_y(-?\d+)\.jpg$")


def main():
    print(f"Scanning {OUT_DIR} for existing tiles...", flush=True)
    count = 0
    with open(INDEX_PATH, "w") as out:
        # Flat files
        with os.scandir(OUT_DIR) as it:
            for entry in it:
                if entry.is_file() and PATTERN.match(entry.name):
                    st = entry.stat()
                    if st.st_size > 100:
                        out.write(entry.name + "\n")
                        count += 1
                        if count % 50000 == 0:
                            print(f"  {count}...", flush=True)
        # Subdirectory files
        for sub in OUT_DIR.iterdir():
            if not (sub.is_dir() and sub.name.startswith("x")):
                continue
            for sub2 in sub.iterdir():
                if not (sub2.is_dir() and sub2.name.startswith("y")):
                    continue
                with os.scandir(sub2) as it:
                    for entry in it:
                        if entry.is_file() and PATTERN.match(entry.name):
                            st = entry.stat()
                            if st.st_size > 100:
                                out.write(entry.name + "\n")
                                count += 1
                                if count % 50000 == 0:
                                    print(f"  {count}...", flush=True)
    print(f"Wrote {count} entries to {INDEX_PATH}", flush=True)


if __name__ == "__main__":
    main()
