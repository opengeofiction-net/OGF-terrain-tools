#!/usr/bin/env python3
"""
OpenGeofiction New User Patrol - Territorial Compliance & Classification

Checks if new users are mapping only in permitted territories
(only "open to all" = blue territories) and classifies users by behavior.

Uses SQLite cache for changeset data to avoid re-fetching from the API.

Usage:
    bin/userPatrol.py                       # Run patrol with console report
    bin/userPatrol.py --json                # Save JSON reports (detailed + summary)
    bin/userPatrol.py --db-report           # Show changeset cache statistics
    bin/userPatrol.py --user <id>           # Patrol specific user
    bin/userPatrol.py --scp <target>        # Save JSON + scp to remote target

JSON Output (--json):
    - var/new_users_patrol.json: Detailed report with full violation data (flagged users only)
    - var/new_users_patrol_summary.json: Flat summary matching new_users.json format (flagged users only)
      Fields: name, profile, block_status, classification, violations, notified, notes

SCP Output (--scp <target>):
    Automatically enables --json and copies both JSON files to the specified
    scp target (e.g., ogf@util.ogf:/opt/opengeofiction/sync-to-ogf/utility)
"""

import json
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import sqlite3
import os
import sys
import time
import re
import math
import subprocess
from datetime import datetime, timezone
from collections import defaultdict

# ─── Configuration ───────────────────────────────────────────────────────────

OGF_API = "https://opengeofiction.net/api/0.6"
USER_AGENT = "OGF-Patrol/1.0 (adminbot)"
REFERER = "https://opengeofiction.net/"

NEW_USERS_URL = "https://data.opengeofiction.net/utility/new_users.json"
TERRITORY_URL = "https://data.opengeofiction.net/utility/territory.json"
TERRITORY_STATUS_URL = "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Territory_administration?action=raw"
NOTIFIED_USERS_URL = "https://wiki.opengeofiction.net/index.php/Help:New_user_patrol?action=raw"

# Paths relative to script location
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "..", "var", "patrol.db")
PATROL_DIR = os.path.join(SCRIPT_DIR, "..", "var")

# ─── User Permission Cache ───────────────────────────────────────────────────

permission_cache = {}  # username -> set of allowed editors

def fetch_user_allowed_editors(username):
    """
    Fetch a user's profile page and extract the list of users they've given permission to edit their territory.
    
    Looks for patterns like:
    "People currently working on Cygagon who I have given them permission to:"
    followed by a list of usernames.
    
    Returns:
        set of usernames who have permission, or empty set if none found.
    """
    if username in permission_cache:
        return permission_cache[username]
    
    try:
        # Fetch profile page
        url = f"https://opengeofiction.net/user/{urllib.parse.quote(username)}"
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")
        
        # Extract the description section - look for permission patterns
        allowed = set()
        
        # Find the permission section and extract names from following <p> tags
        # Pattern: "People currently working on X who I have given them permission to:"
        permission_patterns = [
            r'[Pp]eople (currently )?working (on [^:<>]+ )?who I have given (them )?permission to:',
            r'[Pp]eople working here with permission:',
            r'[Aa]llowed editors:',
            r'[Pp]ermission granted to:',
            r'[Uu]sers with permission:',
            r'[Mm]appers with permission:',
        ]
        
        for pattern in permission_patterns:
            match = re.search(pattern, html)
            if match:
                # Get the HTML after the match
                start = match.end()
                following_html = html[start:start+2000]  # Get next 2000 chars
                
                # Extract text from <p> tags
                p_tags = re.findall(r'<p>([^<]+)</p>', following_html)
                for text in p_tags:
                    name = text.strip()
                    # Filter out empty lines and non-name text
                    if name and len(name) > 1 and len(name) < 50:
                        # Skip common non-name words that might appear
                        if name.lower() not in ['and', 'or', 'the', 'to', 'for', 'with', 'any', 'all', 'currently']:
                            # Skip entries that look like sentences (have periods or are too long)
                            # Usernames typically don't have periods or multiple words with punctuation
                            if '.' not in name or name.count(' ') <= 2:
                                # Additional check: usernames are usually 1-3 words, no sentences
                                if not re.search(r'\.\s*[A-Z]', name):  # No sentence patterns
                                    allowed.add(name)
                break
        
        permission_cache[username] = allowed
        if allowed:
            print(f"  [PERMISSION] {username}: {len(allowed)} allowed editors: {', '.join(sorted(allowed))}")
        return allowed
        
    except Exception as e:
        print(f"  [ERROR] Could not fetch permissions for {username}: {e}")
        permission_cache[username] = set()
        return set()

# ─── HTTP Helpers ────────────────────────────────────────────────────────────

def oget(path):
    """Fetch from the OGF API with proper headers."""
    if path.startswith(OGF_API):
        url = path
    elif path.startswith("/api/"):
        base = "https://opengeofiction.net"
        url = f"{base}{path}"
    else:
        url = f"{OGF_API}/{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        print(f"  [HTTP {e.code}] {url}")
        return None
    except Exception as e:
        print(f"  [ERROR] {url}: {e}")
        return None

def json_get(url):
    """Fetch JSON from a data endpoint."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [JSON ERROR] {url}: {e}")
        return None

# ─── Point-in-Polygon (Ray Casting) ──────────────────────────────────────────

def point_in_polygon(lon, lat, polygon):
    """Check if (lon, lat) is inside a polygon. polygon: list of (lon, lat)."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    x, y = lon, lat
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside

def point_in_polygon_with_holes(lon, lat, outer_ring, holes=None):
    """Check if (lon, lat) is inside a polygon with optional holes."""
    if not point_in_polygon(lon, lat, outer_ring):
        return False
    if holes:
        for hole in holes:
            if point_in_polygon(lon, lat, hole):
                return False
    return True

def parse_territory_polygon(coords):
    """Parse territory.json polygon into (outer_ring, holes)."""
    if not coords or len(coords) < 2:
        return [], []
    if isinstance(coords[0], list) and len(coords[0]) > 0 and isinstance(coords[0][0], list):
        outer = [(c[1], c[0]) for c in coords[0]]
        holes = [[(c[1], c[0]) for c in ring] for ring in coords[1:]]
        return outer, holes
    else:
        outer = [(c[1], c[0]) for c in coords]
        return outer, []

# ─── Data Loading ────────────────────────────────────────────────────────────

def load_new_users():
    """Load and parse the new_users.json list."""
    print("[1/4] Loading new users...")
    data = json_get(NEW_USERS_URL)
    if not data:
        print("  ERROR: Could not load new_users.json")
        return []
    users = []
    for u in data:
        users.append({
            "id": int(u.get("id", 0)),
            "name": u.get("name", ""),
            "changesets_count": int(u.get("changesets", 0)),
            "created": u.get("created", ""),
            "latest": u.get("latest", ""),
            "block_status": u.get("block_status", ""),
            "is_mod": u.get("mod", "N") == "Y",
            "is_admin": u.get("admin", "N") == "Y",
        })
    print(f"  Loaded {len(users)} new users")
    return users

def load_territories():
    """Load territory polygons from territory.json."""
    print("[2/4] Loading territory polygons...")
    data = json_get(TERRITORY_URL)
    if not data:
        print("  ERROR: Could not load territory.json")
        return {}
    territories = {}
    for ogf_id, coords in data.items():
        outer, holes = parse_territory_polygon(coords)
        territories[ogf_id] = {
            "outer_ring": outer,
            "holes": holes,
        }
    print(f"  Loaded {len(territories)} territory polygons")
    return territories

def load_territory_statuses():
    """
    Load territory statuses from the wiki raw JSON.
    Returns dict: rel_id -> {"status", "owner", "ogfId"}
    """
    print("[3/4] Loading territory statuses...")
    data = json_get(TERRITORY_STATUS_URL)
    if data is None:
        print("  WARNING: Could not load territory statuses. Using permissive mode.")
        return {}
    try:
        return _parse_statuses_data(data)
    except Exception as e:
        print(f"  WARNING: Could not parse territory status JSON: {e}")
        return {}

def _parse_statuses_data(data):
    """Parse territory status data from either a list or a dict."""
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = list(data.values())
    else:
        return {}
    result = {}
    for entry in items:
        rel = entry.get("rel")
        if rel is not None:
            result[str(rel)] = {
                "status": entry.get("status", ""),
                "owner": entry.get("owner", None),
                "ogfId": entry.get("ogfId", ""),
                "rel": rel,
            }
    print(f"  Loaded {len(result)} territory statuses")
    return result

def get_permissible_territories(statuses):
    """Return set of territory IDs that are 'open to all' only."""
    return {
        k for k, v in statuses.items()
        if v["status"] == "open to all"
    }

def load_notified_users():
    """
    Parse the contacted users list from the patrol help page.
    Extracts usernames from {{OGF user|USERNAME}} wiki markup.
    Returns a set of username strings.
    """
    print("[4/5] Loading contacted users list...")
    try:
        req = urllib.request.Request(NOTIFIED_USERS_URL, headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://opengeofiction.net/",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        print(f"  [ERROR] Could not load notified users: {e}")
        return set()

    # Extract all {{OGF user|USERNAME}} patterns from the contacted users section
    # The contacted users list is in the first table, before "Please add to the top..."
    # We extract from the whole page since the format is consistent
    usernames = set()
    for match in re.finditer(r'\{\{OGF user\|([^}|]+)\}\}', raw):
        usernames.add(match.group(1).strip())

    # Also try the old pattern {{UserLink|USERNAME}} if it exists
    for match in re.finditer(r'\{\{UserLink\|([^}|]+)\}\}', raw):
        usernames.add(match.group(1).strip())

    print(f"  Loaded {len(usernames)} notified users")
    return usernames

# ─── API Data Fetching ───────────────────────────────────────────────────────

def fetch_user_changesets(user_id):
    """Fetch all changesets for a given user from the OGF API."""
    path = f"/api/0.6/changesets?user={user_id}"
    xml = oget(path)
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
        changesets = []
        for cs in root.findall(".//changeset"):
            comment_tag = cs.find("tag[@k='comment']")
            changesets.append({
                "id": int(cs.get("id", 0)),
                "created_at": cs.get("created_at", ""),
                "closed_at": cs.get("closed_at", ""),
                "changes_count": int(cs.get("changes_count", 0)),
                "min_lat": float(cs.get("min_lat", 0)),
                "min_lon": float(cs.get("min_lon", 0)),
                "max_lat": float(cs.get("max_lat", 0)),
                "max_lon": float(cs.get("max_lon", 0)),
                "comment": comment_tag.get("v", "") if comment_tag is not None else "",
            })
        return changesets
    except ET.ParseError as e:
        print(f"  [PARSE ERROR] changesets for uid {user_id}: {e}")
        return []

def fetch_changeset_nodes(changeset_id, user_id=None):
    """Download and parse all nodes from a changeset. Uses SQLite cache.
    
    Args:
        changeset_id: The changeset ID to fetch
        user_id: Optional user ID for cache storage
    
    Returns:
        List of node dicts with id, lat, lon, changeset_id, tags
    """
    # Check cache first
    cached_nodes = get_cached_changeset(changeset_id)
    if cached_nodes is not None:
        return cached_nodes
    
    # Fetch from API
    path = f"/api/0.6/changeset/{changeset_id}/download"
    xml = oget(path)
    if not xml:
        return []
    
    # Parse the XML
    try:
        root = ET.fromstring(xml)
        seen_ids = set()
        nodes = []
        for node in root.findall(".//node"):
            nid = int(node.get("id", 0))
            lat = float(node.get("lat", 0))
            lon = float(node.get("lon", 0))
            if nid in seen_ids:
                continue
            if lat == 0.0 and lon == 0.0:
                continue
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "lat": lat,
                "lon": lon,
                "changeset_id": int(node.get("changeset", 0)),
                "tags": {t.get("k"): t.get("v") for t in node.findall("tag")},
            })
        
        # Cache the parsed result
        cache_changeset(changeset_id, user_id or 0, nodes)
        return nodes
    except ET.ParseError as e:
        print(f"  [PARSE ERROR] changeset {changeset_id}: {e}")
        return []

# ─── Violation Detection ─────────────────────────────────────────────────────

def check_node_against_territories(lon, lat, territories, permissible, statuses):
    """
    Check a single node against all territories.
    Returns list of (territory_id, status, owner, matched_territory_type).
    Skips "outline" territories as they are covered by other territories.
    """
    violations = []
    for ogf_id, terr in territories.items():
        if not terr["outer_ring"]:
            continue
        # Skip outline territories - they are already covered by other territories
        status_info = statuses.get(ogf_id, {"status": "unknown", "owner": None})
        if status_info["status"] == "outline":
            continue
        if point_in_polygon_with_holes(lon, lat, terr["outer_ring"], terr["holes"]):
            terr_type = "permissible" if ogf_id in permissible else "restricted"
            violations.append((ogf_id, status_info["status"], status_info["owner"], terr_type))
    return violations

def patrol_user(username, user_id, territories, permissible, statuses, notified_users=None):
    """Run patrol for a single user. Returns a report dict."""
    if notified_users is None:
        notified_users = set()
    report = {
        "username": username,
        "user_id": user_id,
        "changesets_fetched": 0,
        "nodes_checked": 0,
        "violations": [],
        "territories_mapped": set(),
        "notified": username in notified_users,
        "notes": [],
    }

    changesets = fetch_user_changesets(user_id)
    report["changesets_fetched"] = len(changesets)

    if not changesets:
        report["notes"].append("No changesets found via API (user may be deleted/purged).")
        return report

    changesets.sort(key=lambda c: c["created_at"])

    for i, cs in enumerate(changesets):
        print(f"  [{username}] Processing changeset {i+1}/{len(changesets)} (c{cs['id']})", end="\r", flush=True)

        nodes = fetch_changeset_nodes(cs["id"], user_id)
        report["nodes_checked"] += len(nodes)

        for node in nodes:
            hits = check_node_against_territories(
                node["lon"], node["lat"],
                territories, permissible, statuses
            )

            for terr_id, status, owner, terr_type in hits:
                report["territories_mapped"].add(terr_id)

                if terr_type == "restricted":
                    report["violations"].append({
                        "changeset_id": cs["id"],
                        "node_id": node["id"],
                        "lat": node["lat"],
                        "lon": node["lon"],
                        "territory_id": terr_id,
                        "territory_status": status,
                        "territory_owner": owner,
                        "node_tags": node.get("tags", {}),
                    })

        time.sleep(0.1)

    return report

# ─── User Classification ────────────────────────────────────────────────────

def classify_user(user_info, report):
    """
    Classify a user based on heuristics.
    
    Returns:
        classification: "good_faith" | "needs_review" | "suspicious" | "likely_vandal"
        reasons: list of reason strings
        confidence: "low" | "medium" | "high"
    """
    reasons = []
    score = 0  # Negative = more suspicious, positive = more good faith
    
    username = user_info["name"]
    changeset_count = user_info["changesets_count"]
    api_changesets = report["changesets_fetched"]
    violations = len(report["violations"])
    nodes_checked = report["nodes_checked"]
    notes = report["notes"]
    
    # ── 1. Username heuristics ──────────────────────────────────────────
    
    # Check for common patterns
    is_all_caps = username == username.upper() and any(c.isalpha() for c in username)
    is_suspicious_name = bool(re.search(r'(fuck|shit|bitch|nigger|faggot|asshole|porn|sex|xxx)', username, re.IGNORECASE))
    is_very_short = len(username) <= 3
    is_all_numbers = username.isdigit()
    is_random_chars = bool(re.search(r'(.)\1{3,}', username))  # 4+ repeating chars like "aaaa"
    has_emoji = any(ord(c) > 127 for c in username)
    
    if is_suspicious_name:
        reasons.append(f"Suspicious username pattern: '{username}'")
        score -= 3
    elif is_all_caps and len(username) > 8:
        reasons.append("All-caps username (often trolling)")
        score -= 1
    elif is_very_short:
        reasons.append("Very short username (potential throwaway)")
        score -= 1
    elif is_all_numbers:
        reasons.append("Numeric-only username (potential auto-generated)")
        score -= 1
    
    # ── 2. Changeset behavior ───────────────────────────────────────────
    
    if api_changesets == 0 and changeset_count > 0:
        reasons.append(f"API reports 0 changesets but new_users.json shows {changeset_count} (user deleted/purged?)")
        score -= 2
    elif api_changesets > 100:
        reasons.append(f"Very high changeset count: {api_changesets} (potential mass edit bot)")
        score -= 2
    elif api_changesets == 0 and changeset_count == 0:
        reasons.append("No changesets in either source (possibly just registered)")
        score += 0  # Neutral - could be brand new
    
    # ── 3. Territorial violations ───────────────────────────────────────
    
    if violations > 0:
        # Categorize violation types and check for explicit permissions
        violation_types = defaultdict(int)
        permitted_violations = defaultdict(int)  # violations that have explicit permission
        
        for v in report["violations"]:
            status = v["territory_status"]
            owner = v.get("territory_owner")
            
            # Check if user has explicit permission from territory owner
            has_permission = False
            if owner and status in ["owned", "marked for withdrawal"]:
                allowed_editors = fetch_user_allowed_editors(owner)
                if username in allowed_editors:
                    has_permission = True
                    permitted_violations[status] += 1
                    continue  # Skip this violation - it's permitted
            
            violation_types[status] += 1
        
        # Report permitted edits (no penalty)
        for status, count in permitted_violations.items():
            reasons.append(f"Mapped {count} nodes in {status} territories with owner permission (no penalty)")
        
        # Report actual violations
        for status, count in violation_types.items():
            if status == "reserved" or status == "archived":
                reasons.append(f"Mapped {count} nodes in {status} territories")
                score -= 3
            elif status == "owned" or status == "available" or status == "marked for withdrawal":
                suffix = " (needs admin approval)" if status == "available" else ""
                reasons.append(f"Mapped {count} nodes in {status} territories{suffix}")
                score -= 2
            elif status == "collaborative":
                reasons.append(f"Mapped {count} nodes in collaborative territories")
                score -= 1
            else:
                reasons.append(f"Mapped {count} nodes in '{status}' territory")
                score -= 1
    
    if report["territories_mapped"] and len(report["territories_mapped"]) > 3:
        reasons.append(f"Mapped across {len(report['territories_mapped'])} different territories (unusual spread)")
        score -= 1
    
   # ── 4. Node density (mass edits in tiny area) ──────────────────────

    if nodes_checked > 500 and len(report["territories_mapped"]) <= 1:
        if report["territories_mapped"]:
            terr_id = list(report["territories_mapped"])[0]
            info = statuses_global.get(terr_id, {})
            if info.get("status") == "open to all":
                score += 1  # Concentrated editing in blue territory is fine
    
    # ── 5. Activity patterns ────────────────────────────────────────────
    
    if changeset_count > 0 and not user_info["latest"]:
        reasons.append("No latest activity recorded (possibly dormant/abandoned)")
        score -= 1
    
    # ── Classification ──────────────────────────────────────────────────
    
    # If there are no violations and no strong signals, classify as good_faith
    has_any_issue = (
        violations > 0 or
        len(report["notes"]) > 0 or
        score <= -2
    )
    
    if not has_any_issue:
        return {
            "classification": "good_faith",
            "reasons": ["No issues detected"],
            "confidence": "medium",
            "score": 0,
        }
    
    if score <= -5:
        classification = "likely_vandal"
        confidence = "high" if score <= -7 else "medium"
    elif score <= -3:
        classification = "suspicious"
        confidence = "medium"
    elif score <= -1:
        classification = "needs_review"
        confidence = "medium"
    else:
        classification = "good_faith"
        confidence = "high"
    
    # Admins/mods are always good faith
    if user_info["is_mod"] or user_info["is_admin"]:
        classification = "good_faith"
        confidence = "high"
        reasons = ["Admin/Mod account"]
    
    return {
        "classification": classification,
        "reasons": reasons,
        "confidence": confidence,
        "score": score,
    }

# Global for use in classify_user (set by main)
statuses_global = {}

# ─── Database Storage ────────────────────────────────────────────────────────

def init_db():
    """Initialize the local SQLite database for changeset caching."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS changeset_cache (
            changeset_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            nodes_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_changeset_user ON changeset_cache(user_id);
        CREATE INDEX IF NOT EXISTS idx_changeset_fetched ON changeset_cache(fetched_at);
    """)
    conn.commit()
    
    # Clean up old cache entries (older than 60 days)
    c.execute("DELETE FROM changeset_cache WHERE fetched_at < datetime('now', '-60 days')")
    deleted = c.rowcount
    conn.commit()
    if deleted > 0:
        print(f"  [CACHE] Cleaned up {deleted} old cache entries")
    
    conn.close()
    return conn

def get_cached_changeset(changeset_id):
    """Retrieve a changeset's nodes from the cache.
    
    Returns:
        List of node dicts if cached, None if not found.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT nodes_json FROM changeset_cache WHERE changeset_id = ?", (changeset_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return None

def cache_changeset(changeset_id, user_id, nodes):
    """Store a changeset's nodes in the cache.
    
    Args:
        changeset_id: The changeset ID
        user_id: The user who made the changeset
        nodes: List of node dicts to cache
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO changeset_cache (changeset_id, user_id, nodes_json, fetched_at) VALUES (?, ?, ?, datetime('now'))",
        (changeset_id, user_id, json.dumps(nodes))
    )
    conn.commit()
    conn.close()

# ─── Report Generation ───────────────────────────────────────────────────────

def print_report(report, classification):
    """Print a formatted report for a single user."""
    cls = classification["classification"]
    
    # Status emoji
    emoji = {
        "good_faith": "✅",
        "needs_review": "🔍",
        "suspicious": "⚠️",
        "likely_vandal": "🚫",
    }.get(cls, "❓")
    
    if cls == "good_faith" and not report["violations"] and not report["notes"]:
        print(f"  [{emoji}] {report['username']} - No issues ({report['nodes_checked']} nodes checked across {report['changesets_fetched']} changesets)")
        return
    
    print(f"\n  {'='*60}")
    print(f"  USER: {report['username']} (ID: {report['user_id']})")
    print(f"  Changesets: {report['changesets_fetched']} | Nodes checked: {report['nodes_checked']}")
    print(f"  Territories mapped: {len(report['territories_mapped'])}")
    print(f"  {'='*60}")
    
    # Classification
    print(f"  📊 CLASSIFICATION: {cls.upper()} ({classification['confidence']} confidence)")
    for reason in classification["reasons"]:
        print(f"     • {reason}")
    
    if report["violations"]:
        print(f"\n  [ALERT] {len(report['violations'])} territorial violations:")
        unique_violations = {}
        for v in report["violations"]:
            terr_id = v["territory_id"]
            if terr_id not in unique_violations:
                unique_violations[terr_id] = v
        for terr_id, v in list(unique_violations.items())[:10]:
            owner = v["territory_owner"] or "Unknown"
            print(f"    - Territory {terr_id} ({v['territory_status']}, owner: {owner})")
            print(f"      Node: ({v['lat']:.6f}, {v['lon']:.6f}) in changeset {v['changeset_id']}")
        if len(unique_violations) > 10:
            print(f"    ... and {len(unique_violations) - 10} more territories")
    
    if report["notes"]:
        for note in report["notes"]:
            print(f"  [NOTE] {note}")

def print_summary(all_reports, all_classifications):
    """Print a summary of all user reports and classifications."""
    total_violations = sum(len(r["violations"]) for r in all_reports)
    users_with_violations = sum(1 for r in all_reports if r["violations"])
    
    # Count classifications
    class_counts = defaultdict(int)
    for cls in all_classifications:
        class_counts[cls["classification"]] += 1
    
    print(f"\n{'='*60}")
    print(f"  PATROL SUMMARY")
    print(f"  Users scanned: {len(all_reports)}")
    print(f"  Total violations: {total_violations}")
    print(f"  Users with violations: {users_with_violations}")
    print(f"  {'='*60}")
    
    print(f"\n  CLASSIFICATIONS:")
    for cls_name in ["likely_vandal", "suspicious", "needs_review", "good_faith"]:
        count = class_counts.get(cls_name, 0)
        if count > 0:
            emoji = {"likely_vandal": "🚫", "suspicious": "⚠️", "needs_review": "🔍", "good_faith": "✅"}.get(cls_name, "")
            print(f"    {emoji} {cls_name}: {count}")
    
    # Users requiring attention
    flagged = [
        (r, c) for r, c in zip(all_reports, all_classifications)
        if c["classification"] in ("likely_vandal", "suspicious", "needs_review") or r["violations"]
    ]
    flagged.sort(key=lambda x: x[1]["score"])
    
    if flagged:
        print(f"\n  USERS REQUIRING ATTENTION:")
        # Header row
        print(f"    {'User':<25} {'Classification':<20} {'Violations':<12} {'Notified':<10} Notes")
        print(f"    {'─'*25} {'─'*20} {'─'*12} {'─'*10} {'─'*30}")
        for report, cls in flagged:
            viol_count = len(report["violations"])
            unique_terr = len(set(v["territory_id"] for v in report["violations"])) if report["violations"] else 0
            notified_str = "✅" if report["notified"] else "—"
            notes = ', '.join(cls['reasons'][:3])
            print(f"    {report['username']:<25} [{cls['classification']}]:{' '*(18-len(cls['classification']))} {viol_count:<12} {notified_str:<10} ({notes})")

# ─── JSON Output ─────────────────────────────────────────────────────────────

def generate_json_output(all_reports, all_classifications, user_info):
    """Generate detailed structured JSON output."""
    output = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "total_users": len(all_reports),
        "classifications": {},
        "flagged_users": [],
    }
    
    # Count classifications
    for cls in all_classifications:
        name = cls["classification"]
        if name not in output["classifications"]:
            output["classifications"][name] = 0
        output["classifications"][name] += 1
    
    # Detailed flagged users
    for report, cls, u_info in zip(all_reports, all_classifications, user_info):
        # URL-encode username for profile link
        username_encoded = urllib.parse.quote(report["username"], safe='')
        
        entry = {
            "username": report["username"],
            "user_id": report["user_id"],
            "profile": f"https://opengeofiction.net/user/{username_encoded}",
            "classification": cls["classification"],
            "confidence": cls["confidence"],
            "score": cls["score"],
            "reasons": cls["reasons"],
            "changesets_fetched": report["changesets_fetched"],
            "nodes_checked": report["nodes_checked"],
            "territories_mapped": list(report["territories_mapped"]),
            "violations_count": len(report["violations"]),
            "notified": get_notified_status(report, cls),
            "notes": report["notes"],
        }
        if report["violations"]:
            unique_violations = {}
            for v in report["violations"]:
                terr_id = v["territory_id"]
                if terr_id not in unique_violations:
                    unique_violations[terr_id] = v
            entry["territory_violations"] = unique_violations
        
        if cls["classification"] in ("likely_vandal", "suspicious", "needs_review") or report["violations"]:
            output["flagged_users"].append(entry)
    
    output["flagged_users"].sort(key=lambda x: x["score"])
    
    return output

def get_notified_status(report, cls):
    """Determine notified status as 'Y', 'N', or ''.
    
    - 'Y' if user is in the contacted list
    - 'N' if classification is 'suspicious' or 'needs_review' (and not contacted)
    - '' otherwise (good_faith users, or flagged users already contacted)
    """
    if report.get("notified", False):
        return "Y"
    elif cls["classification"] in ("suspicious", "needs_review"):
        return "N"
    else:
        return ""

def generate_summary_json(all_reports, all_classifications, user_info):
    """Generate flat summary JSON matching new_users.json format.
    
    Only includes flagged users (violations or suspicious/needs_review/likely_vandal).
    Output fields: name, profile, block_status, classification, violations, notified, notes
    """
    summary = []
    
    for report, cls, u_info in zip(all_reports, all_classifications, user_info):
        # Only include flagged users (same criteria as detailed JSON)
        if cls["classification"] not in ("likely_vandal", "suspicious", "needs_review") and not report["violations"]:
            continue
        
        # Join notes into a simple string
        notes_list = cls["reasons"] if cls["reasons"] else [report["notes"]] if report["notes"] else []
        notes_str = "; ".join(notes_list) if notes_list else ""
        
        # URL-encode username for profile link
        username_encoded = urllib.parse.quote(report["username"], safe='')
        
        entry = {
            "name": report["username"],
            "profile": f"https://opengeofiction.net/user/{username_encoded}",
            "block_status": u_info.get("block_status", ""),
            "classification": cls["classification"],
            "violations": len(report["violations"]),
            "notified": get_notified_status(report, cls),
            "notes": notes_str,
        }
        summary.append(entry)
    
    # Sort by classification severity (worst first)
    severity_order = {"likely_vandal": 0, "suspicious": 1, "needs_review": 2, "good_faith": 3}
    summary.sort(key=lambda x: (severity_order.get(x["classification"], 4), -x["violations"]))
    
    return summary

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    # Parse command line arguments
    mode = "run"
    target_user = None
    json_output = False
    scp_target = None
    
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--user" and i + 1 < len(args):
            target_user = int(args[i + 1])
            mode = "single"
            i += 2
        elif args[i] == "--json":
            json_output = True
            i += 1
        elif args[i] == "--scp" and i + 1 < len(args):
            scp_target = args[i + 1]
            json_output = True  # --scp implies --json
            i += 2
        elif args[i] == "--db-report":
            mode = "db-report"
            i += 1
        else:
            i += 1
    
    if mode == "db-report":
        if not os.path.exists(DB_PATH):
            print("No local database found. Run patrol first.")
            return
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(*) as total_cached,
                   COUNT(DISTINCT user_id) as unique_users,
                   MIN(fetched_at) as oldest,
                   MAX(fetched_at) as newest
            FROM changeset_cache
        """)
        row = c.fetchone()
        print(f"\n{'='*60}")
        print(f"  CHANGESET CACHE STATISTICS")
        print(f"{'='*60}")
        print(f"  Total changesets cached: {row[0]}")
        print(f"  Unique users: {row[1]}")
        print(f"  Oldest cache entry: {row[2]}")
        print(f"  Newest cache entry: {row[3]}")
        
        c.execute("""
            SELECT user_id, COUNT(*) as cs_count
            FROM changeset_cache
            GROUP BY user_id
            ORDER BY cs_count DESC
            LIMIT 10
        """)
        rows = c.fetchall()
        if rows:
            print(f"\n  Top 10 users by cached changesets:")
            print(f"  {'User ID':<15} {'Changesets':>12}")
            print(f"  {'-'*27}")
            for r in rows:
                print(f"  {r[0]:<15} {r[1]:>12}")
        conn.close()
        return
    
    print("=" * 60)
    print("  OpenGeofiction New User Patrol")
    print("=" * 60)
    
    # Initialize the changeset cache
    init_db()
    
    users = load_new_users()
    territories = load_territories()
    statuses = load_territory_statuses()
    permissible = get_permissible_territories(statuses)
    notified_users = load_notified_users()
    statuses_global = statuses  # For use in classify_user
    print(f"  Permissible (blue) territories: {len(permissible)}")
    
    all_reports = []
    all_classifications = []
    
    # Filter to target user if specified
    if target_user is not None:
        users = [u for u in users if u["id"] == target_user]
    
    for i, user in enumerate(users):
        print(f"\n[{i+1}/{len(users)}] Patrolling {user['name']} (ID: {user['id']})...", flush=True)
        report = patrol_user(user["name"], user["id"], territories, permissible, statuses, notified_users)
        all_reports.append(report)
        classification = classify_user(user, report)
        all_classifications.append(classification)
        print_report(report, classification)
    
    print_summary(all_reports, all_classifications)
    
    # Generate JSON output if requested
    if json_output:
        print(f"\n{'='*60}")
        print(f"  JSON OUTPUT")
        print(f"{'='*60}")
        
        # Save detailed JSON
        detailed_path = os.path.join(PATROL_DIR, "new_users_patrol.json")
        json_output_data = generate_json_output(all_reports, all_classifications, users)
        with open(detailed_path, "w", encoding="utf-8") as f:
            json.dump(json_output_data, f, indent=2, ensure_ascii=False)
        print(f"  Detailed report saved: {detailed_path}")
        
        # Save summary JSON (flat format matching new_users.json)
        summary_path = os.path.join(PATROL_DIR, "new_users_patrol_summary.json")
        summary_output = generate_summary_json(all_reports, all_classifications, users)
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary_output, f, indent=2, ensure_ascii=False)
        print(f"  Summary report saved: {summary_path}")
        
        # SCP to remote target if specified
        if scp_target:
            print(f"\n  SCP TO: {scp_target}")
            try:
                # SCP detailed report
                cmd = ["scp", detailed_path, scp_target]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0:
                    print(f"  ✓ Uploaded: new_users_patrol.json")
                else:
                    print(f"  ✗ Failed: new_users_patrol.json - {result.stderr.strip()}")
                
                # SCP summary report
                cmd = ["scp", summary_path, scp_target]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0:
                    print(f"  ✓ Uploaded: new_users_patrol_summary.json")
                else:
                    print(f"  ✗ Failed: new_users_patrol_summary.json - {result.stderr.strip()}")
            except subprocess.TimeoutExpired:
                print(f"  ✗ SCP timed out after 60 seconds")
            except FileNotFoundError:
                print(f"  ✗ scp command not found")
            except Exception as e:
                print(f"  ✗ SCP error: {e}")

if __name__ == "__main__":
    main()
