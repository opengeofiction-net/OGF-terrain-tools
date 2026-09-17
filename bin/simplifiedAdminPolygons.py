#!/usr/bin/env python3
"""
simplifiedAdminPolygons.py — Python rewrite of bin/simplifiedAdminPolygons.pl

Creates simplified administrative boundary polygons from OSM/Overpass data
for use on the OpenGeofiction map and wiki.

Usage:
    simplifiedAdminPolygons.py [options] [osm_file.osm]

Options:
    -ogf       Use ogf:id as polygon keys instead of numeric relation ID
    -ds DS     Dataset: "test" or empty (default)
    -od DIR    Output directory for JSON files (default: /tmp)
    -copyto DIR  Publish directory to copy output files into
    -h         Show help

If no OSM file is given, data is fetched from the Overpass API.

Dependencies: Python 3.7+ standard library only.
For better performance, optionally install lxml (python3-lxml).
"""

import sys
import os
import re
import json
import math
import time
import shutil
import logging
import argparse
import configparser
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default Overpass URL (overridable via ogftools.conf)
OVERPASS_URL = "https://overpass.opengeofiction.net/api/interpreter"

# Overpass query templates per dataset
QUERIES = {
    "default": """[timeout:60][maxsize:80000000];
(
  (relation["boundary"="administrative"]["admin_level"="2"];
   relation["boundary"="administrative"]["admin_level"="3"]["ogf:id"~"^(UL08c|UL16)-[0-9]{2}$"];
   relation["boundary"="administrative"]["admin_level"="4"]["ogf:id"~"^(AR(045|047|060|120)|UL10|UL08c)-[0-9]{2}$"];
   relation["boundary"="timezone"]["timezone"];);
  >;
);
out;""",
    "test": r"""[timeout:90][maxsize:80000000];
(
  (relation["boundary"="administrative"]["ogf:id"~"^AR120-0[1-9]$"];);
  >;
);
out;""",
    }

# Territory administration URLs
TERRITORY_URLS = {
    "default": "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Territory_administration?action=raw",
    "test": "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Territory_administration/test?action=raw",
}

COMPUTATION_ZOOM = {"default": 6, "test": 6}
OUTFILE_NAMES = {"default": "ogf_polygons", "test": "test_polygons"}

# Known issues — relation IDs to ignore during verification
# Example: VERIFY_IGNORE = {459229: "TA250"}
VERIFY_IGNORE = {}

# Simplification thresholds to compute: the first is primary, saved as territory.json and errors output
THRESHOLDS = [50, 1, 10, 200]

# Smallest ring, in square pixels at the computation zoom, worth emitting when a
# ring is preserved below the threshold (see the polygon assembly). Below this it
# is simplification debris or a way that never closed into a ring.
MIN_RING_PX_AREA = 0.25

# Floor for the iterative re-simplification of a preserved ring: threshold/2,
# threshold/4 and so on down to this, after which the ring is emitted
# unsimplified. Halving once is not always enough - a ring only a few pixels
# across collapses to a line at threshold/2 as well, and would be lost.
MIN_RING_PX_THRESHOLD = 1.0

# Web Mercator constants
WGS84_SEMI_MAJOR = 6378137.0
WGS84_ECCEN_SQ = 0.00669437999014
WGS84_SEMI_MINOR = 6356752.3142
METERS_PER_DEGREE_LAT = WGS84_SEMI_MAJOR * math.pi / 180.0
WEB_MERC_HALF = 20037508.3427892  # PI * 6378137

# Tile settings for "OGF" layer at zoom level (Mercator projection)
TILE_SIZE = 256

# Overpass retry settings
MAX_RETRIES = 3
RETRY_BACKOFF = 10

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config():
    """Read ogftools.conf from ~/.ogf/ or /etc/ogf/.
    Supports Config::General flat key=value, <Section> tags, and [Section] INI.
    """
    paths = [
        Path.home() / ".ogf" / "ogftools.conf",
        Path("/etc/ogf/ogftools.conf"),
    ]
    for p in paths:
        if p.exists():
            content = p.read_text()
            # Convert Config::General <Section> format to INI [Section] format
            content = re.sub(r'<\s*(\w+)\s*>', r'[\1]', content)
            content = re.sub(r'</\s*\w+\s*>', '', content)
            # If no section header, prepend [DEFAULT]
            if not re.search(r'^\s*\[', content, re.MULTILINE):
                content = '[DEFAULT]\n' + content

            cp = configparser.ConfigParser()
            cp.read_string(content)
            result = {}
            for section in cp.sections():
                for k, v in cp.items(section):
                    result[k] = v
            return result
    return {}

# ---------------------------------------------------------------------------
# Web Mercator projection
# ---------------------------------------------------------------------------

def merc_pixel_transform(zoom):
    """Returns a function that converts (lon, lat) to pixel coordinates at given zoom."""
    world_size = TILE_SIZE * (2 ** zoom)
    cX = world_size / (2 * WEB_MERC_HALF)
    tX = world_size / 2
    cY = -world_size / (2 * WEB_MERC_HALF)
    tY = world_size / 2

    def lonlat_to_pixel(lon, lat):
        # Mercator projection
        x = lon * WEB_MERC_HALF / 180.0
        lat_rad = math.radians(lat)
        y = math.log(math.tan(math.pi / 4 + lat_rad / 2)) * WEB_MERC_HALF / math.pi
        # Transform to pixel space
        px = x * cX + tX
        py = y * cY + tY
        return (px, py)

    return lonlat_to_pixel

# ---------------------------------------------------------------------------
# OSM XML parsing (via lxml iterparse)
# ---------------------------------------------------------------------------

def parse_osm(osm_path):
    """Parse an OSM XML file into nodes, ways, relations dicts.

    Uses lxml if available, otherwise falls back to stdlib xml.etree.
    """
    nodes = {}
    ways = {}
    relations = {}

    current_way = None
    current_way_nodes = []
    current_rel = None
    current_rel_members = []
    current_rel_tags = {}

    # Try lxml first for better performance
    try:
        from lxml import etree
        # lxml: filter by tag on 'end' events only (children fire before parents)
        context = etree.iterparse(str(osm_path), events=('start', 'end'),
                                  tag=('node', 'way', 'relation', 'nd', 'member', 'tag'))
        use_lxml = True
    except ImportError:
        from xml.etree.ElementTree import iterparse
        # stdlib: need 'start' events to know which way/rel we're inside
        context = iterparse(str(osm_path), events=('start', 'end'))
        use_lxml = False

    for event, elem in context:
        tag = elem.tag

        if event == 'start':
            if tag == 'way':
                current_way = int(elem.get('id'))
                current_way_nodes = []
            elif tag == 'relation':
                current_rel = int(elem.get('id'))
                current_rel_members = []
                current_rel_tags = {}

        elif event == 'end':
            if tag == 'node':
                nid = int(elem.get('id'))
                lat = float(elem.get('lat'))
                lon = float(elem.get('lon'))
                nodes[nid] = (lat, lon)

            elif tag == 'nd':
                ref = elem.get('ref')
                if ref and current_way is not None:
                    current_way_nodes.append(int(ref))

            elif tag == 'way':
                if current_way is not None:
                    ways[current_way] = current_way_nodes

            elif tag == 'member':
                ref = elem.get('ref')
                if ref and current_rel is not None:
                    current_rel_members.append((
                        elem.get('type', ''),
                        elem.get('role', ''),
                        int(ref)
                    ))

            elif tag == 'tag':
                k = elem.get('k')
                v = elem.get('v')
                if k and v and current_rel is not None:
                    current_rel_tags[k] = v

            elif tag == 'relation':
                if current_rel is not None and current_rel_members:
                    relations[current_rel] = {
                        'tags': current_rel_tags,
                        'members': current_rel_members,
                    }

        # Free memory after end events
        if event == 'end':
            if use_lxml:
                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]
            else:
                elem.clear()

    del context
    return nodes, ways, relations

# ---------------------------------------------------------------------------
# Visvalingam-Whyatt simplification
# ---------------------------------------------------------------------------

def triangle_area(p1, p2, p3):
    """Absolute cross product (parallelogram area) of triangle."""
    return abs((p1[0] - p2[0]) * (p3[1] - p2[1]) -
               (p1[1] - p2[1]) * (p3[0] - p2[0]))

def alg_visvalingam_whyatt(points, threshold, index_only=True):
    """Simplify a line using Visvalingam-Whyatt algorithm.

    Args:
        points: List of (x, y) tuples
        threshold: Minimum effective area to keep
        index_only: If True, return indices rather than points

    Returns:
        List of indices into the original points array
    """
    n = len(points)
    if n <= 2:
        return list(range(n))

    INF = 999999
    # Each entry: [idx, size]
    area = []
    area.append([0, INF])  # first point always kept
    for i in range(1, n - 1):
        area.append([i, triangle_area(points[i - 1], points[i], points[i + 1])])
    area.append([n - 1, INF])  # last point always kept

    while True:
        # Find minimum area (skip inf-marked endpoints if they're in the middle)
        min_area = float('inf')
        min_pos = -1
        for i in range(1, len(area) - 1):
            if area[i][1] < min_area:
                min_area = area[i][1]
                min_pos = i

        if min_area > threshold or len(area) <= 2:
            break

        # Remove this vertex
        area.pop(min_pos)

        # Recompute affected neighbors
        if min_pos > 1 and min_pos < len(area):
            p1 = points[area[min_pos - 2][0]]
            p2 = points[area[min_pos - 1][0]]
            p3 = points[area[min_pos][0]]
            area[min_pos - 1][1] = triangle_area(p1, p2, p3)

        if min_pos > 0 and min_pos < len(area) - 1:
            p1 = points[area[min_pos - 1][0]]
            p2 = points[area[min_pos][0]]
            p3 = points[area[min_pos + 1][0]]
            area[min_pos][1] = triangle_area(p1, p2, p3)

    return [a[0] for a in area]

# ---------------------------------------------------------------------------
# Way topology: connect ways into sequences
# ---------------------------------------------------------------------------

def build_way_sequence(way_info):
    """Connect ways via shared endpoints. Aggressively retries until convergence.

    Args:
        way_info: dict of way_id -> {'nodes': [node_id, ...], 'start': node_id, 'end': node_id}

    Returns:
        list of dicts, each with 'nodes', 'start', 'end'
    """
    # Work on a copy
    hWays = {wid: dict(info, ways=[wid]) for wid, info in way_info.items()}
    if not hWays:
        return []

    # Keep retrying until no more merges happen (fixes hash-order-dependent orphaning)
    while True:
        ptStart = {}  # node_id -> wid  (way starting at this node)
        ptEnd = {}    # node_id -> wid  (way ending at this node)
        merged = False

        for wid in list(hWays.keys()):
            if wid not in hWays:
                continue
            w = hWays[wid]
            if len(w['nodes']) < 2:
                del hWays[wid]
                continue

            idS = w['nodes'][0]
            idE = w['nodes'][-1]

            # Closed way (ring): track but don't merge
            if idS == idE:
                ptStart[idS] = wid
                continue

            # Reversal check: align direction if another way starts at my start
            # or ends at my end
            if idS in ptStart or idE in ptEnd:
                oid = ptStart.get(idS) or ptEnd.get(idE)
                if oid is not None and oid in hWays:
                    o = hWays[oid]
                    o['nodes'].reverse()
                    o['ways'].reverse()
                    idS2, idE2 = o['nodes'][0], o['nodes'][-1]
                    if idE2 in ptStart:
                        del ptStart[idE2]
                    if idS2 in ptEnd:
                        del ptEnd[idS2]
                    ptStart[idS2] = oid
                    ptEnd[idE2] = oid

            # Merge A: another way ends at my start → it absorbs me
            if idS in ptEnd:
                oid = ptEnd[idS]
                if oid in hWays:
                    o = hWays[oid]
                    o['nodes'].extend(w['nodes'][1:])  # skip duplicate start
                    o['ways'].extend(w['ways'])
                    del hWays[wid]
                    del ptEnd[idS]
                    # Clean up absorbed way's other endpoint
                    if w['nodes'][-1] in ptEnd and ptEnd.get(w['nodes'][-1]) == wid:
                        del ptEnd[w['nodes'][-1]]
                    if w['nodes'][0] in ptStart and ptStart.get(w['nodes'][0]) == wid:
                        del ptStart[w['nodes'][0]]
                    wid = oid
                    w = o
                    idS, idE = w['nodes'][0], w['nodes'][-1]
                    merged = True

            # Merge B: I absorb a way starting at my end
            if idS != idE and idE in ptStart:
                oid = ptStart[idE]
                if oid in hWays:
                    o = hWays[oid]
                    w['nodes'].extend(o['nodes'][1:])  # skip duplicate
                    w['ways'].extend(o['ways'])
                    idE = w['nodes'][-1]
                    del hWays[oid]
                    # Clean up absorbed way's other endpoint
                    if o['nodes'][-1] in ptEnd and ptEnd.get(o['nodes'][-1]) == oid:
                        del ptEnd[o['nodes'][-1]]
                    if o['nodes'][0] in ptStart and ptStart.get(o['nodes'][0]) == oid:
                        del ptStart[o['nodes'][0]]
                    merged = True

            # Track endpoints
            if w['nodes'][0] != w['nodes'][-1]:
                ptStart[w['nodes'][0]] = wid
                ptEnd[w['nodes'][-1]] = wid

        if not merged:
            break

    result = []
    for wid, w in hWays.items():
        result.append({
            'nodes': w['nodes'],
            'ways': w['ways'],
            'start': w['nodes'][0],
            'end': w['nodes'][-1],
        })
    return result

# ---------------------------------------------------------------------------
# Overpass API
# ---------------------------------------------------------------------------

def fetch_overpass(query, min_size):
    """Fetch data from Overpass API with retries."""
    url = OVERPASS_URL
    data = query.encode('utf-8')

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=data)
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = resp.read().decode('utf-8')

            if not result.startswith('<?xml'):
                logging.warning("Overpass query attempt %d: non-XML response", attempt)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                return None

            if len(result) < min_size:
                preview = result[:800]
                logging.warning("Overpass query attempt %d: too small (%d bytes): %s",
                              attempt, len(result), preview)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                return None

            return result

        except urllib.error.URLError as e:
            logging.warning("Overpass query attempt %d: %s", attempt, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)

    return None

# ---------------------------------------------------------------------------
# Territory list
# ---------------------------------------------------------------------------

def fetch_territories(dataset):
    """Fetch territory administration JSON from wiki."""
    url = TERRITORY_URLS.get(dataset, TERRITORY_URLS['default'])
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        logging.error("Failed to fetch territories: %s", e)
        return []

# ---------------------------------------------------------------------------
# Bounding rectangle
# ---------------------------------------------------------------------------

def simplify_ring_points(node_ids, nodes, to_pixel, threshold):
    """Simplify a ring given as a list of node IDs, at the given threshold.

    Returns a list of [lat, lon] points, or None if any node is missing from the
    OSM data. Used to re-emit a ring that would otherwise be dropped, at a finer
    threshold, so small exclaves survive.
    """
    pts = []
    for nid in node_ids:
        if nid not in nodes:
            return None
        lat, lon = nodes[nid]
        pts.append(to_pixel(lon, lat))

    if len(pts) > 2:
        indices = alg_visvalingam_whyatt(pts, threshold, index_only=True)
    else:
        indices = list(range(len(pts)))

    out = []
    for i in indices:
        lat, lon = nodes[node_ids[i]]
        out.append([lat, lon])
    return out


def is_real_ring(points, to_pixel):
    """True if the points are a polygon rather than a line or a sub-pixel stub.

    Two nodes mean a way that never closed into a ring; a bounding box under
    MIN_RING_PX_AREA is simplification debris. Both used to be dropped and still are.
    """
    if not points or len(points) < 3:
        return False
    xs = []
    ys = []
    for lat, lon in points:
        px, py = to_pixel(lon, lat)
        xs.append(px)
        ys.append(py)
    return (max(xs) - min(xs)) * (max(ys) - min(ys)) >= MIN_RING_PX_AREA


def preserve_ring(node_ids, nodes, to_pixel, threshold):
    """Re-emit a ring that would otherwise be dropped below the threshold.

    Iteratively re-simplifies at threshold/2, threshold/4, ... until the ring
    survives as a real polygon, and finally falls back to the unsimplified ring -
    a ring smaller than the smallest triangle the simplifier keeps has no coarser
    form that still closes. Returns [lat, lon] points, or None if the ring is not
    a polygon at any resolution (a way that never closed, or debris).
    """
    t = threshold / 2.0
    while t >= MIN_RING_PX_THRESHOLD:
        points = simplify_ring_points(node_ids, nodes, to_pixel, t)
        if is_real_ring(points, to_pixel):
            return points
        t /= 2.0

    points = simplify_ring_points(node_ids, nodes, to_pixel, 0.0)
    if is_real_ring(points, to_pixel):
        logging.debug("ring preserved at full resolution (%d node(s))", len(node_ids))
        return points
    return None


def bounding_rectangle(way_nodes, nodes, to_pixel):
    """Compute bounding rectangle of a way in pixel coordinates."""
    if not way_nodes:
        return [0, 0, 0, 0]
    lat, lon = nodes[way_nodes[0]]
    px, py = to_pixel(lon, lat)
    xMin = xMax = px
    yMin = yMax = py

    for nid in way_nodes:
        lat, lon = nodes.get(nid, (0, 0))
        px, py = to_pixel(lon, lat)
        if px < xMin: xMin = px
        if px > xMax: xMax = px
        if py < yMin: yMin = py
        if py > yMax: yMax = py

    return [xMin, yMin, xMax, yMax]

# ---------------------------------------------------------------------------
# Polygon verification
# ---------------------------------------------------------------------------

def verify_polygon(poly):
    """Check if a polygon is valid. Returns error string or empty string."""
    if not poly or not poly[0]:
        return "Empty Polygon"

    p0 = poly[0]
    if isinstance(p0, list) and p0 and isinstance(p0[0], list):
        # Multipolygon: check each ring
        errors = []
        for ring in poly:
            err = verify_polygon(ring)
            if err:
                errors.append(err)
        return "\n".join(errors)
    else:
        # Single polygon ring
        if not poly:
            return "Empty Polygon"
        x0, y0 = poly[0]
        x1, y1 = poly[-1]
        if x0 == x1 and y0 == y1:
            return ""
        else:
            return (f"Polygon not closed (gap between "
                    f"[https://opengeofiction.net/#map=16/{x0}/{y0} A] and "
                    f"[https://opengeofiction.net/#map=16/{x1}/{y1} B])")

# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def write_polygon_json(filepath, polygons):
    """Write the polygon JSON in the exact format expected by the Perl script."""
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write("{\n")
        keys = sorted(polygons.keys())
        for ki, key in enumerate(keys):
            val = polygons[key]
            comma = "," if ki < len(keys) - 1 else ""

            # Check if this is a multipolygon (list of rings) or single ring
            if val and isinstance(val[0][0], (list, tuple)):
                # Multipolygon: key -> list of rings
                f.write(f'"{key}": [\n')
                for ri, ring in enumerate(val):
                    ring_comma = "," if ri < len(val) - 1 else ""
                    ring_json = json.dumps(ring, separators=(',', ':'))
                    f.write(f"  {ring_json}{ring_comma}\n")
                f.write(f"]{comma}\n")
            else:
                # Single ring: key -> [points]
                val_json = json.dumps(val, separators=(',', ':'))
                f.write(f'"{key}": {val_json}{comma}\n')
        f.write("}\n")

def run(args):
    """Main entry point."""
    # Load config
    global OVERPASS_URL
    conf = load_config()
    if 'overpass_url' in conf:
        OVERPASS_URL = conf['overpass_url']

    # Dataset
    dataset = args.get('ds', '')
    if dataset and dataset not in ('test',):
        sys.exit(f'Unknown dataset: "{dataset}"')

    output_dir = args.get('od', '/tmp')
    copy_to = args.get('copyto', '')
    use_ogf_id = args.get('ogf', False)
    osm_file = args.get('osm_file', '')

    # Housekeeping: clean old .osm files
    try:
        now = time.time()
        for f in Path(output_dir).glob("admin_polygons_*.osm"):
            if now - f.stat().st_mtime > 86400:  # 1 day
                logging.info("deleting: %s", f)
                f.unlink()
    except OSError:
        pass

    # Territory list and OSM source
    zoom = COMPUTATION_ZOOM.get(dataset, 6)
    outfile_name = OUTFILE_NAMES.get(dataset, 'ogf_polygons')

    if False:
        territories = []
    else:
        territories = fetch_territories(dataset)

    query = QUERIES.get(dataset, QUERIES['default'])

    # Get OSM data
    if not osm_file:
        ts = datetime.now(timezone.utc).strftime('%y%m%d_%H%M%S')
        osm_file = f"{output_dir}/admin_polygons_{ts}.osm"
        if not os.path.exists(osm_file):
            logging.info("Fetching from Overpass...")
            data = fetch_overpass(query, 10_000_000)
            if data is None:
                logging.error("Overpass fetch failed")
                sys.exit(1)
            os.makedirs(os.path.dirname(osm_file) or '.', exist_ok=True)
            with open(osm_file, 'w', encoding='utf-8') as f:
                f.write(data)
            logging.info("Overpass data saved to %s (%d bytes)", osm_file, len(data))

    if not os.path.exists(osm_file):
        sys.exit(1)

    # Parse OSM
    logging.info("Parsing OSM file: %s", osm_file)
    t0 = time.time()
    nodes, ways, relations = parse_osm(osm_file)
    logging.info("Parsed %d nodes, %d ways, %d relations in %.1fs",
                 len(nodes), len(ways), len(relations), time.time() - t0)

    # Build way-to-relations reverse index
    way_to_rels = {}
    for rid, rel in relations.items():
        rel_uid = f"R|{rid}"
        for mtype, mrole, mref in rel['members']:
            if mtype == 'way':
                way_uid = f"W|{mref}"
                way_to_rels.setdefault(way_uid, {})[rel_uid] = True

    # Build shared-border groups
    # Group ways by the sorted set of relation UIDs they belong to
    shared_borders = {}
    for wid, w in ways.items():
        way_uid = f"W|{wid}"
        rels = sorted(way_to_rels.get(way_uid, {}).keys())
        key = ":".join(rels)
        num = len(rels)
        if num > 2:
            logging.warning("way %d is element of %d relations", wid, num)

        shared_borders.setdefault(key, []).append(wid)

    # Setup projection
    to_pixel = merc_pixel_transform(zoom)

    # Process each threshold
    for threshold in THRESHOLDS:
        logging.info("Processing threshold=%d", threshold)

        # ctx3: the "output" context — simplified ways grouped by relation
        ctx3_ways = {}    # neg_id -> {'nodes': [...], 'id': neg_id}
        ctx3_rels = {}    # rel_id -> list of way neg_ids
        neg_ct = 0

        # Process each shared-border group
        for key, way_ids in shared_borders.items():
            # relIds: extract numeric relation IDs from "R|379:R|459" format
            if key:
                rel_ids = [int(s[2:]) for s in key.split(":")]
            else:
                rel_ids = []

            if not rel_ids:
                continue

            # Get way info for this group
            group_way_info = {}
            for wid in way_ids:
                if wid in ways:
                    nds = ways[wid]
                    if len(nds) >= 2:
                        group_way_info[wid] = {
                            'nodes': list(nds),
                            'start': nds[0],
                            'end': nds[-1],
                        }

            if not group_way_info:
                continue

            # Connect ways into sequences
            connected = build_way_sequence(group_way_info)

            # Simplify each connected sequence
            for seq in connected:
                # Project nodes to pixel coordinates
                pixel_pts = []
                node_ids = seq['nodes']
                for nid in node_ids:
                    if nid in nodes:
                        lat, lon = nodes[nid]
                        pixel_pts.append(to_pixel(lon, lat))

                # Visvalingam-Whyatt simplification
                if len(pixel_pts) > 2:
                    indices = alg_visvalingam_whyatt(pixel_pts, threshold, index_only=True)
                else:
                    indices = list(range(len(pixel_pts)))

                # Filter node list
                seq['nodes'] = [node_ids[i] for i in indices]

                # Assign negative ID
                neg_ct -= 1
                way_id = neg_ct
                ctx3_ways[way_id] = {
                    'nodes': seq['nodes'],
                    # Pre-simplification nodes, so a ring that would be dropped
                    # can be rebuilt at threshold/2 instead of being lost.
                    'orig_nodes': list(node_ids),
                    'id': way_id,
                }

                # Add to each relation in this group
                for rid in rel_ids:
                    ctx3_rels.setdefault(rid, []).append(way_id)

        # Build polygons for each relation
        polygons = {}
        preserved_small_rings = 0

        for rid, way_ids in ctx3_rels.items():
            # Get all simplified ways for this relation
            rel_way_info = {}
            rel_way_info_fine = {}
            for wid in way_ids:
                if wid in ctx3_ways:
                    w = ctx3_ways[wid]
                    rel_way_info[wid] = {
                        'nodes': list(w['nodes']),
                        'start': w['nodes'][0],
                        'end': w['nodes'][-1],
                    }
                    orig = w['orig_nodes']
                    rel_way_info_fine[wid] = {
                        'nodes': list(orig),
                        'start': orig[0],
                        'end': orig[-1],
                    }

            if not rel_way_info:
                continue

            # Connect them into closed rings
            outer_ways = build_way_sequence(rel_way_info)
            if not outer_ways:
                continue

            # Build polygon rings
            poly_rings = []
            for ow in outer_ways:
                # Bounding rectangle
                rect = bounding_rectangle(ow['nodes'], nodes, to_pixel)
                rect_area = abs(rect[2] - rect[0]) * abs(rect[3] - rect[1])

                # Extract lat/lon points
                points = []
                for nid in ow['nodes']:
                    if nid in nodes:
                        lat, lon = nodes[nid]
                        points.append([lat, lon])
                    else:
                        logging.warning("invalid node %d (possible Overpass problem)", nid)

                poly_rings.append({
                    'rect_area': rect_area,
                    'points': points,
                    'ways': ow['ways'],
                })

            if not poly_rings:
                continue

            # Sort by area descending, preserve largest, filter small ones
            poly_rings.sort(key=lambda r: r['rect_area'], reverse=True)

            # A ring below the threshold is normally dropped as map clutter, but
            # it can be real territory - an exclave, such as Plevia's Isole
            # Libeccie, or an enclave belonging to a neighbour - so instead of
            # dropping it, preserve_ring() re-simplifies it at threshold/2,
            # threshold/4, ... and finally unsimplified, until it is a polygon
            # again. The rebuild is lazy: only rings that would actually be
            # dropped pay for it, which is a handful across the whole dataset.
            for i in range(1, len(poly_rings)):
                if poly_rings[i]['rect_area'] < threshold:
                    ring_ways = [w for w in poly_rings[i]['ways'] if w in rel_way_info_fine]
                    seqs = build_way_sequence({w: rel_way_info_fine[w] for w in ring_ways})
                    if len(seqs) == 1:
                        fine = preserve_ring(seqs[0]['nodes'], nodes, to_pixel, threshold)
                    else:
                        fine = None
                        logging.debug("rel %s: dropped ring assembled %d sequence(s) "
                                      "instead of 1 - not preserved", rid, len(seqs))
                    if fine:
                        poly_rings[i] = {
                            'rect_area': poly_rings[i]['rect_area'],
                            'points': fine,
                        }
                        preserved_small_rings += 1
                    else:
                        logging.debug("rel %s: dropped ring (%d way(s), coarse area %.4f) is "
                                      "not a polygon at any resolution - dropped",
                                      rid, len(ring_ways), poly_rings[i]['rect_area'])
                        poly_rings[i] = None

            result = [r['points'] for r in poly_rings if r is not None]
            if len(result) == 1:
                result = result[0]

            # Determine key
            if use_ogf_id:
                if rid in relations:
                    rel_key = relations[rid]['tags'].get('ogf:id', '')
                else:
                    rel_key = ''
                if not rel_key:
                    logging.warning("Unexpected error: no relation key (rel=%d)", rid)
                    continue
            else:
                rel_key = rid

            polygons[rel_key] = result

        # Verify against territory list
        errors = []
        if territories:
            for terr in territories:
                ogf_id = terr.get('ogfId', '')
                rid = terr.get('rel', 0)
                err_text = ''

                required = ['ogfId', 'name', 'rel', 'status', 'owner', 'deadline', 'comment', 'constraints']
                if not all(k in terr for k in required):
                    err_text = 'Territory JSON missing ogfId, name, rel, status, owner, deadline, comment, or constraints'
                elif rid in polygons:
                    err_text = verify_polygon(polygons[rid])
                else:
                    err_text = 'Missing polygon'

                sys.stderr.write(ogf_id)
                if err_text:
                    sys.stderr.write(f" {err_text}")
                    if rid not in VERIFY_IGNORE:
                        errors.append({
                            '_ogfId': ogf_id,
                            '_rel': rid,
                            '_text': err_text,
                        })
                sys.stderr.write("\n")

        # Write errors JSON (only for the primary threshold, traditionally 50)
        if threshold == THRESHOLDS[0]:
            err_file = f"{output_dir}/{outfile_name}_errors.json"
            with open(err_file, 'w', encoding='utf-8') as f:
                json.dump(errors, f, indent=2)
                f.write("\n")

            if copy_to and os.path.isdir(copy_to):
                pub_file = f"{copy_to}/territory_errors.json"
                shutil.copy2(err_file, pub_file)

        if errors:
            # Print errors to stderr for debugging
            json.dump(errors, sys.stderr, indent=2)
            sys.stderr.write("\n")
            # Exit on errors (matching Perl behavior)
            sys.exit(1)

        # Write polygon JSON
        poly_file = f"{output_dir}/{outfile_name}_{threshold}.json"
        write_polygon_json(poly_file, polygons)

        if copy_to and os.path.isdir(copy_to):
            pub_file = f"{copy_to}/territory_{threshold}.json"
            shutil.copy2(poly_file, pub_file)
            if threshold == THRESHOLDS[0]:
                pub_file = f"{copy_to}/territory.json"
                shutil.copy2(poly_file, pub_file)

        logging.info("Threshold %d: wrote %d polygons to %s", threshold, len(polygons), poly_file)
        if preserved_small_rings:
            logging.info("Threshold %d: kept %d ring(s) below the threshold, "
                         "re-simplified at %g", threshold, preserved_small_rings,
                         threshold / 2.0)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Create simplified administrative boundary polygons for OpenGeofiction",
        add_help=False,
    )
    parser.add_argument('-h', action='store_true', help='Show help')
    parser.add_argument('-ogf', action='store_true', default=False, help='Use ogf:id as key')
    parser.add_argument('-ds', default='', help='Dataset: test or empty (default)')
    parser.add_argument('-od', default='/tmp', help='Output directory for JSON files')
    parser.add_argument('-copyto', default='', help='Publish directory to copy output files into')
    parser.add_argument('osm_file', nargs='?', default='', help='OSM file to process (optional, fetches from Overpass if omitted)')

    opts = parser.parse_args()

    if opts.h:
        parser.print_help()
        sys.exit(0)

    run({
        'ogf': opts.ogf,
        'ds': opts.ds,
        'od': opts.od,
        'copyto': opts.copyto,
        'osm_file': opts.osm_file,
    })

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main()
