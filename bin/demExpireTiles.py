#!/usr/bin/env python3
#
# The cached tiles which sit over a changed zone - see Admin:Elevation process
#
#   demExpireTiles.py <changed-zones> <style> <min-zoom> [--tile-dir DIR]
#
# Writes z/x/y on stdout, one line per metatile, for render_expired to touch.
#
# Walks the tile cache rather than enumerating the tile space. The space is
# geometric: covering a six by four degree zone takes 12,428 metatiles at z16
# and 795,364 at z19, nearly all of which have never been rendered. The cache
# above z12 holds a few thousand metatiles for the whole planet, so sweeping it
# costs a readdir and answers the question exactly.
#
# One sweep serves every changed zone. On a busy server the cache is the
# expensive thing to walk, and walking it once per build beats once per zone.
#
# mod_tile stores a metatile under a 5 level hash of its origin tile, from
# xyzo_to_meta() in store_file_utils.c:
#
#     hash[i] = ((x & 0x0f) << 4) | (y & 0x0f)   for i in 0..4, x and y >>= 4
#     <tile_dir>/<style>/<z>/<h4>/<h3>/<h2>/<h1>/<h0>.meta
#
# which inverts: each path component carries four bits of x in its high nibble
# and four of y in its low.

import argparse
import math
import os
import re
import sys

METATILE = 8
RENDERD_CONF = '/etc/renderd.conf'
DEFAULT_TILE_DIR = '/var/cache/renderd/tiles'
META = re.compile(r'^(\d+)\.meta$')


def tile_dir_from_renderd(path=RENDERD_CONF):
    """tile_dir out of renderd.conf, or the compiled in default."""
    try:
        with open(path) as f:
            for line in f:
                key, _, value = line.partition('=')
                if key.strip().lower() == 'tile_dir':
                    return value.strip()
    except OSError:
        pass
    return DEFAULT_TILE_DIR


def meta_to_xy(hashes):
    """The five path components, outermost first, back to the origin tile."""
    x = y = 0
    for i, h in enumerate(reversed(hashes)):
        x |= (h >> 4) << (4 * i)
        y |= (h & 0x0f) << (4 * i)
    return x, y


def xtile(lon, z):
    return (lon + 180.0) / 360.0 * 2 ** z


def ytile(lat, z):
    r = math.radians(max(min(lat, 85.05112878), -85.05112878))
    return (1 - math.asinh(math.tan(r)) / math.pi) / 2 * 2 ** z


def read_zones(path):
    """<zone> <minlon> <minlat> <maxlon> <maxlat> per line. Zones recorded
    before the footprint was written carry a name only - they cannot be placed,
    and are counted rather than guessed at."""
    boxes, unplaced = [], []
    with open(path) as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            if len(parts) < 5:
                unplaced.append(parts[0])
                continue
            boxes.append((parts[0], *(float(v) for v in parts[1:5])))
    return boxes, unplaced


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('changed_zones')
    ap.add_argument('style')
    ap.add_argument('min_zoom', type=int)
    ap.add_argument('--tile-dir', default=None)
    args = ap.parse_args()

    tile_dir = args.tile_dir or tile_dir_from_renderd()
    boxes, unplaced = read_zones(args.changed_zones)
    if unplaced:
        print(f'  no footprint, not expired: {" ".join(unplaced)}',
              file=sys.stderr)
    if not boxes:
        return

    root = os.path.join(tile_dir, args.style)
    if not os.path.isdir(root):
        sys.exit(f'{root}: no tile cache for {args.style}')

    # the tile range each zone covers, per zoom, worked out once
    ranges = {}
    walked = emitted = 0

    for zdir in sorted(os.listdir(root)):
        if not zdir.isdigit():
            continue
        z = int(zdir)
        if z < args.min_zoom:
            continue
        ranges[z] = [(int(xtile(w, z)), int(ytile(n, z)),
                      int(xtile(e, z)), int(ytile(s, z)))
                     for _, w, s, e, n in boxes]

        for dirpath, _, files in os.walk(os.path.join(root, zdir)):
            hashes = None
            for name in files:
                m = META.match(name)
                if not m:
                    continue
                if hashes is None:
                    # the four directories above, plus this file
                    parts = dirpath.split(os.sep)[-4:]
                    if len(parts) != 4 or not all(p.isdigit() for p in parts):
                        break
                    hashes = [int(p) for p in parts]
                walked += 1
                x, y = meta_to_xy(hashes + [int(m.group(1))])
                for x0, y0, x1, y1 in ranges[z]:
                    # a metatile counts if any of its tiles fall inside
                    if (x + METATILE > x0 and x <= x1
                            and y + METATILE > y0 and y <= y1):
                        print(f'{z}/{x}/{y}')
                        emitted += 1
                        break

    print(f'  {emitted} of {walked} cached metatiles at z{args.min_zoom} and '
          f'above sit over a changed zone', file=sys.stderr)


if __name__ == '__main__':
    main()
