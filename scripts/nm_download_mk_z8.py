#!/usr/bin/env python3
"""NorthMacedonia — MK 2023 cadastre orthophoto, zoom 8 (2.24 m/px), tile grid.

The MK cadastre is a GeoWebCache tile cache (e-uslugi.katastar.gov.mk): it only
serves 256x256 grid-aligned tiles at fixed pyramid resolutions, NOT arbitrary
GetMap. So the high-grade national ortho is downloaded as a zoom-8 tile grid
(2.24 m/px — just finer than the 2.8 m/px patch output, so no upsampling, and
~100x fewer tiles than the Skopje pilot's zoom 11). The tile set is restricted to
tiles whose box intersects the MK national boundary polygon (built by
nm_prepare_boundary equivalent step into mk_z8_tiles.json) — the AL/GR/BG/RS/XK
margins are covered by ESRI instead, so we never download blank cadastre tiles.

Tiles -> .sandbox/textures_nm/mk_z8/x{tx//256}/y{ty//256}/z8_x{tx}_y{ty}.jpg
Then nm_build_mk_vrt.py builds a single VRT over them for per-patch warping.

Async, high concurrency, resumable (skips present tiles). Deterministic tile set.

Run:  python scripts/nm_download_mk_z8.py [tx_min tx_max ty_min ty_max] [concurrency]
      (no range args = full MK tile set from mk_z8_tiles.json)
"""
import os
import sys
import json
import time
import asyncio
import aiohttp
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".sandbox" / "textures_nm" / "mk_z8"
OUT.mkdir(parents=True, exist_ok=True)
LOG = ROOT / "logs" / "nm_mk_z8.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
TILESET = ROOT / ".sandbox" / "textures_nm" / "mk_z8_tiles.json"

ORIGIN_X = 7397000.634424793
ORIGIN_Y = 4521901.793180252
RESOLUTIONS = [560, 280, 140, 70, 35, 17.92, 8.96, 4.48, 2.24, 1.12, 0.56, 0.28, 0.14]
ZOOM = 8
RES = RESOLUTIONS[ZOOM]
TILE_PX = 256
TILE_M = TILE_PX * RES  # 573.44 m

BASE_URL = "https://e-uslugi.katastar.gov.mk/geo/proxy/gwc/wms"
HEADERS = {"Referer": "https://e-uslugi.katastar.gov.mk/"}


def tile_path(tx, ty):
    return OUT / f"x{tx // 256}" / f"y{ty // 256}" / f"z{ZOOM}_x{tx}_y{ty}.jpg"


def tile_url(tx, ty):
    minx = ORIGIN_X + tx * TILE_M
    miny = ORIGIN_Y + ty * TILE_M
    params = {
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
        "LAYERS": "APP_DATA:ORTOFOTO_2023", "STYLES": "",
        "FORMAT": "image/jpeg", "TILED": "true", "RESIZE": "resize",
        "GRIDSET": "MSCS6316", "SRS": "EPSG:6316",
        "BBOX": f"{minx},{miny},{minx + TILE_M},{miny + TILE_M}",
        "WIDTH": TILE_PX, "HEIGHT": TILE_PX,
    }
    return f"{BASE_URL}?{urlencode(params)}"


def _log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


async def dl(session, tx, ty, sem, stats):
    p = tile_path(tx, ty)
    if p.exists() and p.stat().st_size > 100:
        stats["cached"] += 1
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    url = tile_url(tx, ty)
    async with sem:
        for attempt in range(5):
            try:
                async with session.get(url, headers=HEADERS,
                                       timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 200:
                        ct = r.headers.get("Content-Type", "")
                        data = await r.read()
                        if ct.startswith("image") and len(data) > 100:
                            p.write_bytes(data)
                            stats["ok"] += 1
                            return
                    await asyncio.sleep(0.3 * (attempt + 1))
            except Exception:
                await asyncio.sleep(0.3 * (attempt + 1))
    stats["failed"] += 1


async def main():
    ts = json.loads(TILESET.read_text())
    tiles = [tuple(t) for t in ts["tiles"]]
    if len(sys.argv) >= 5:
        txa, txb, tya, tyb = (int(sys.argv[i]) for i in range(1, 5))
        tiles = [(x, y) for (x, y) in tiles if txa <= x <= txb and tya <= y <= tyb]
        conc = int(sys.argv[5]) if len(sys.argv) > 5 else 192
    else:
        conc = int(sys.argv[1]) if len(sys.argv) > 1 else 192
    total = len(tiles)
    open(LOG, "a").write(f"\n=== START MK z8 {total} tiles conc={conc} "
                         f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    _log(f"MK z8 ({RES} m/px): {total} tiles, concurrency {conc} -> {OUT}")

    stats = {"ok": 0, "failed": 0, "cached": 0}
    sem = asyncio.Semaphore(conc)
    connector = aiohttp.TCPConnector(limit=conc, limit_per_host=conc,
                                     ttl_dns_cache=300, keepalive_timeout=30)
    t0 = time.time()
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [asyncio.create_task(dl(session, x, y, sem, stats)) for x, y in tiles]
        done = 0
        for coro in asyncio.as_completed(tasks):
            await coro
            done += 1
            if done % 5000 == 0 or done == total:
                el = time.time() - t0
                rate = done / el if el else 0
                eta = (total - done) / rate if rate else 0
                _log(f"[{done}/{total}] ok={stats['ok']} cached={stats['cached']} "
                     f"failed={stats['failed']} | {rate:.0f} t/s | ETA {eta:.0f}s")
    el = time.time() - t0
    _log(f"DONE MK z8 in {el:.0f}s. ok={stats['ok']} cached={stats['cached']} "
         f"failed={stats['failed']} / {total}")


if __name__ == "__main__":
    asyncio.run(main())
