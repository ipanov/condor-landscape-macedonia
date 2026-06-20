#!/usr/bin/env python3
"""Migrate flat orthophoto tile storage to subdirectory layout."""
import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / ".sandbox" / "textures_mk2023_z11"
PATTERN = re.compile(r"z11_x(-?\d+)_y(-?\d+)\.(jpg|err)$")


def main():
    files = []
    with os.scandir(OUT_DIR) as it:
        for entry in it:
            if entry.is_file() and PATTERN.match(entry.name):
                files.append(entry.path)
    total = len(files)
    print(f"Migrating {total} files to subdirectories...", flush=True)

    for i, src in enumerate(files):
        name = os.path.basename(src)
        m = PATTERN.match(name)
        tx, ty = int(m.group(1)), int(m.group(2))
        dst_dir = OUT_DIR / f"x{tx // 256}" / f"y{ty // 256}"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / name
        shutil.move(src, dst)
        if i % 10000 == 0 and i > 0:
            print(f"  {i}/{total} ({100*i/total:.1f}%)", flush=True)
    print(f"Migration done: {total} files moved.", flush=True)


if __name__ == "__main__":
    main()
