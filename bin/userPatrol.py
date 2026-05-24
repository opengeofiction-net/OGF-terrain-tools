#!/usr/bin/env python3
"""
OpenGeofiction New User Patrol - Territorial Compliance & Classification

Checks if new users are mapping only in permitted territories
("open to all" = blue, and collaborative territories) and classifies users by behavior.

Uses SQLite cache for changeset data to avoid re-fetching from the API.

Usage:
    bin/userPatrol.py                       # Run patrol with console report
    bin/userPatrol.py --json                # Save JSON reports (detailed + summary)
    bin/userPatrol.py --db-report           # Show changeset cache statistics
    bin/userPatrol.py --user <id>           # Patrol specific user
    bin/userPatrol.py --scp <target>        # Save JSON + scp to remote target
    bin/userPatrol.py --notify              # Send notification messages to flagged users
    bin/userPatrol.py --notify --dry-run    # Show what would be sent without sending

JSON Output (--json):
    - var/new_users_patrol.json: Detailed report with full violation data (flagged users only)
      Fields: username, user_id, profile, classification, confidence, score, reasons, latest, changesets_fetched, nodes_checked, territories_mapped, violations_count, notified, notes, territory_violations
    - var/new_users_patrol_summary.json: Flat summary matching new_users.json format (flagged users only)
      Fields: name, profile, block_status, classification, latest, violations, notified, notes

SCP Output (--scp <target>):
    Automatically enables --json and copies both JSON files to the specified
    scp target (e.g., ogf@util.ogf:/opt/opengeofiction/sync-to-ogf/utility)

Bot Control:
    --notify and --scp require {{permission|yes}} on User:Brothie wiki page.
    If the page cannot be loaded or the marker is absent, these actions are skipped.
    The patrol check and JSON generation still run regardless.
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
import hashlib
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
BOT_CONTROL_URL = "https://wiki.opengeofiction.net/index.php/User:Brothie?action=raw"

# Paths relative to script location
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "..", "var", "patrol.db")
PATROL_DIR = os.path.join(SCRIPT_DIR, "..", "var")

# Credentials path
CREDENTIALS_PATH = os.path.expanduser("~/ogf-user.env")

# Buffer around territory boundaries (degrees). Nodes within this distance
# of a territory edge are considered inside, to avoid false positives on
# boundary nodes due to rounding or slight polygon inaccuracies.
# ~0.01 deg ≈ 1.1 km near the equator. Covers the maximum inward
# deviation introduced by the Visvalingam-Whyatt simplification
# (zoom 6, threshold 100) used in simplifiedAdminPolygons.pl.
TERRITORY_BUFFER_DEG = 0.01

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

# ─── OGF Messaging ───────────────────────────────────────────────────────────

def load_ogf_credentials():
    """Load OGF credentials from the credentials file."""
    credentials = {}
    if os.path.exists(CREDENTIALS_PATH):
        with open(CREDENTIALS_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    credentials[key.strip()] = value.strip()
    return credentials

def ogf_login(username, password):
    """
    Log in to OGF and return a session cookie.
    OGF uses web session login (not OAuth2) for messaging.
    Returns: (session_cookie, csrf_token) or (None, None) on failure.
    """
    try:
        import http.cookiejar
        
        # Create cookie jar and opener
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
        opener.addheaders = [("User-Agent", USER_AGENT)]
        
        # First, get the login page to extract CSRF token
        login_url = "https://opengeofiction.net/login"
        req = urllib.request.Request(login_url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html",
        })
        with opener.open(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")
            # Extract CSRF token from the login form
            csrf_match = re.search(r'name="authenticity_token" value="([^"]+)"', html)
            if not csrf_match:
                print(f"  [LOGIN ERROR] Could not find CSRF token")
                return None, None
            csrf_token = csrf_match.group(1)
        
        # Perform login using the same opener (preserves cookies)
        login_data = urllib.parse.urlencode({
            "username": username,
            "password": password,
            "authenticity_token": csrf_token,
        }).encode("utf-8")
        
        req = urllib.request.Request(login_url, data=login_data, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": login_url,
        })
        
        try:
            resp = opener.open(req, timeout=15)
            resp.read()
        except urllib.error.HTTPError as e:
            print(f"  [LOGIN ERROR] HTTP {e.code}: Login failed")
            return None, None
        
        # Extract session cookie
        session_cookie = None
        for cookie in cookie_jar:
            if cookie.name == "_osm_session":
                session_cookie = f"{cookie.name}={cookie.value}"
                break
        
        if not session_cookie:
            print(f"  [LOGIN ERROR] No session cookie received")
            return None, None
        
        return session_cookie, csrf_token
        
    except Exception as e:
        print(f"  [LOGIN ERROR] {e}")
        return None, None

def get_csrf_token(session_cookie):
    """
    Get a fresh CSRF token from a page while authenticated.
    """
    try:
        # Fetch a page with a form (e.g., message form)
        url = "https://opengeofiction.net/message/new"
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html",
            "Cookie": session_cookie,
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")
            csrf_match = re.search(r'name="authenticity_token" value="([^"]+)"', html)
            if csrf_match:
                return csrf_match.group(1)
            print(f"  [CSRF ERROR] Could not find CSRF token")
            return None
    except Exception as e:
        print(f"  [CSRF ERROR] {e}")
        return None

def send_ogf_message(session_cookie, recipient_username, subject, body, sender_display_name):
    """
    Send a message to an OGF user.
    
    Args:
        session_cookie: The session cookie string (e.g., "_osm_session=abc123")
        recipient_username: The username to send the message to
        subject: Message subject
        body: Message body (Markdown format)
        sender_display_name: The sender's display name (for signature in body)
    
    Returns:
        True if successful, False otherwise.
    """
    try:
        # First, get the message form page to extract CSRF token
        form_url = f"https://opengeofiction.net/message/new/{urllib.parse.quote(recipient_username)}"
        req = urllib.request.Request(form_url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html",
            "Cookie": session_cookie,
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")
            
            # Extract CSRF token
            csrf_match = re.search(r'name="authenticity_token" value="([^"]+)"', html)
            if not csrf_match:
                print(f"  [MESSAGE ERROR] Could not find CSRF token for {recipient_username}")
                return False
            csrf_token = csrf_match.group(1)
        
        # Send the message to /messages
        # Note: display_name should be the RECIPIENT's name (from the form)
        # The sender is identified by the session cookie
        message_data = urllib.parse.urlencode({
            "utf8": "✓",
            "message[title]": subject,
            "message[body]": body,
            "display_name": recipient_username,  # Recipient's name (from form)
            "authenticity_token": csrf_token,
            "commit": "Send",
        }).encode("utf-8")
        
        req = urllib.request.Request("https://opengeofiction.net/messages", data=message_data, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": form_url,
            "Cookie": session_cookie,
        })
        
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                # Check if redirected to inbox (success)
                if "inbox" in resp.url.lower():
                    return True
                response_html = resp.read().decode("utf-8")
                # Check for success indicators
                if "Message sent" in response_html or "Your message has been sent" in response_html:
                    return True
                # Check if we're still on the form page (error)
                if "message[body]" in response_html or "message_title" in response_html:
                    print(f"  [MESSAGE ERROR] Message may not have been sent for {recipient_username}")
                    return False
                return True  # Assume success if no obvious error
        except urllib.error.HTTPError as e:
            print(f"  [MESSAGE ERROR] HTTP {e.code} for {recipient_username}")
            return False
        
    except Exception as e:
        print(f"  [MESSAGE ERROR] {e}")
        return False

# ─── Wiki Editing (MediaWiki API) ────────────────────────────────────────────

WIKI_URL = "https://wiki.opengeofiction.net"
WIKI_API_URL = f"{WIKI_URL}/api.php"

def wiki_api_request(session, params, method="GET"):
    """
    Make a request to the MediaWiki API.
    
    Args:
        session: tuple of (cookie_jar, opener) from wiki_login
        params: dict of API parameters
        method: "GET" or "POST"
    
    Returns:
        Parsed JSON response as dict
    """
    cookie_jar, opener = session
    url = WIKI_API_URL
    
    if method == "GET":
        query = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{url}?{query}")
    else:
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
        })
    
    with opener.open(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wiki_login(username, password):
    """
    Log in to the OGF wiki using the MediaWiki API.
    Returns: (cookie_jar, opener) or (None, None) on failure.
    """
    try:
        import http.cookiejar
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
        opener.addheaders = [
            ("User-Agent", USER_AGENT),
            ("Accept", "application/json"),
        ]
        session = (cookie_jar, opener)
        
        # Step 1: Get login token
        params = {
            "action": "query",
            "meta": "tokens",
            "type": "login",
            "format": "json",
        }
        data = wiki_api_request(session, params)
        login_token = data.get("query", {}).get("tokens", {}).get("logintoken")
        if not login_token:
            print(f"  [WIKI LOGIN ERROR] Could not get login token")
            return None, None
        
        # Step 2: Login with credentials
        params = {
            "action": "login",
            "lgname": username,
            "lgpassword": password,
            "lgtoken": login_token,
            "format": "json",
        }
        data = wiki_api_request(session, params, method="POST")
        
        result = data.get("login", {}).get("result", "")
        if result == "Success":
            print(f"  ✓ Logged in to wiki as {username}")
            return cookie_jar, opener
        
        # Try clientlogin for newer MediaWiki
        if result == "NeedToken":
            params = {
                "action": "clientlogin",
                "username": username,
                "password": password,
                "logintoken": login_token,
                "loginreturnurl": WIKI_URL,
                "format": "json",
            }
            data = wiki_api_request(session, params, method="POST")
            status = data.get("clientlogin", {}).get("status", "")
            if status == "PASS":
                print(f"  ✓ Logged in to wiki as {username}")
                return cookie_jar, opener
            elif status == "UI":
                msg = data.get("clientlogin", {}).get("message", "Unknown")
                print(f"  [WIKI LOGIN ERROR] Login requires UI: {msg}")
                return None, None
            else:
                print(f"  [WIKI LOGIN ERROR] clientlogin failed: {data}")
                return None, None
        
        print(f"  [WIKI LOGIN ERROR] Login failed: {data}")
        return None, None
        
    except urllib.error.HTTPError as e:
        print(f"  [WIKI LOGIN ERROR] HTTP {e.code}")
        return None, None
    except Exception as e:
        print(f"  [WIKI LOGIN ERROR] {e}")
        return None, None


def wiki_get_csrf_token(session):
    """Get CSRF token from the wiki API."""
    params = {
        "action": "query",
        "meta": "tokens",
        "type": "csrf",
        "format": "json",
    }
    data = wiki_api_request(session, params)
    return data.get("query", {}).get("tokens", {}).get("csrftoken")


def wiki_get_page_content(session, page_title):
    """Get the raw content of a wiki page via API."""
    params = {
        "action": "query",
        "titles": page_title,
        "prop": "revisions",
        "rvprop": "content",
        "format": "json",
    }
    data = wiki_api_request(session, params)
    
    pages = data.get("query", {}).get("pages", {})
    page_id = list(pages.keys())[0]
    
    if page_id == "-1":
        return None  # Page doesn't exist
    
    return pages[page_id].get("revisions", [{}])[0].get("*", "")


def wiki_edit_page(session, page_title, content, edit_summary):
    """
    Edit a wiki page via the MediaWiki API.
    
    Args:
        session: tuple of (cookie_jar, opener) from wiki_login
        page_title: The wiki page title
        content: The new page content
        edit_summary: Edit summary
    
    Returns:
        True if successful, False otherwise.
    """
    try:
        # Get CSRF token
        csrf_token = wiki_get_csrf_token(session)
        if not csrf_token:
            print(f"  [WIKI EDIT ERROR] Could not get CSRF token")
            return False
        
        # Edit the page
        params = {
            "action": "edit",
            "title": page_title,
            "text": content,
            "summary": edit_summary,
            "token": csrf_token,
            "format": "json",
            "bot": "1",
        }
        data = wiki_api_request(session, params, method="POST")
        
        if "edit" in data:
            result = data["edit"]
            if result.get("result") == "Success":
                return True
            # Check for specific error messages
            error_info = result.get("info", "Unknown error")
            print(f"  [WIKI EDIT ERROR] {error_info}")
            return False
        
        print(f"  [WIKI EDIT ERROR] Unexpected response: {data}")
        return False
        
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print(f"  [WIKI EDIT ERROR] HTTP 403 - Page '{page_title}' is protected")
        else:
            print(f"  [WIKI EDIT ERROR] HTTP {e.code}")
        return False
    except Exception as e:
        print(f"  [WIKI EDIT ERROR] {e}")
        return False


def add_contacted_user_wiki(session, username, signature_user="wangi"):
    """
    Add a user to the contacted users list on the wiki.
    
    Format: * {{OGF user|USERNAME}} /~~~~
    
    The ~~~~ is MediaWiki markup that expands to the editor's username and timestamp.
    
    Returns:
        True if successful, False otherwise.
    """
    page_title = "Help:New_user_patrol"
    marker = "<!-- ↓↓↓↓↓↓↓↓ ADD NEW ENTRY IMMEDIATELY BELOW THIS LINE ↓↓↓↓↓↓↓↓ -->"
    
    # Get current page content
    old_text = wiki_get_page_content(session, page_title)
    if old_text is None:
        print(f"  [WIKI ERROR] Could not fetch page content for '{page_title}'")
        return False
    
    # Find the insertion marker
    if marker not in old_text:
        print(f"  [WIKI ERROR] Could not find insertion marker in page")
        return False
    
    # Prepare new entry
    # Note: {{{{ and }}}} in f-strings produce {{ and }} in output
    new_entry = f"\n* {{{{OGF user|{username}}}}} /~~~~"
    
    # Insert after the marker
    insert_pos = old_text.find(marker) + len(marker)
    new_text = old_text[:insert_pos] + new_entry + old_text[insert_pos:]
    
    # Edit the page
    edit_summary = f"Adding {username} to contacted users list"
    success = wiki_edit_page(session, page_title, new_text, edit_summary)
    
    if success:
        return True
    return False

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

def _point_to_segment_distance(px, py, x1, y1, x2, y2):
    """
    Compute the minimum distance from point (px, py) to line segment (x1,y1)-(x2,y2).
    Uses squared distance to avoid sqrt until necessary.
    """
    dx = x2 - x1
    dy = y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq == 0:
        # Segment is a point
        return (px - x1) ** 2 + (py - y1) ** 2
    # Project point onto line, clamped to segment
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return (px - proj_x) ** 2 + (py - proj_y) ** 2

def point_near_polygon(lon, lat, polygon, threshold_deg):
    """
    Check if (lon, lat) is within threshold_deg of any edge of the polygon.
    polygon: list of (lon, lat). threshold_deg: degrees.
    """
    n = len(polygon)
    if n < 2:
        return False
    threshold_sq = threshold_deg * threshold_deg
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        dist_sq = _point_to_segment_distance(lon, lat, x1, y1, x2, y2)
        if dist_sq <= threshold_sq:
            return True
    return False

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
    """Return set of territory IDs that are 'open to all' or 'collaborative'."""
    return {
        k for k, v in statuses.items()
        if v["status"] in ("open to all", "collaborative")
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

def check_bot_permission():
    """
    Check the bot control wiki page for {{permission|yes}}.
    Returns True if permission is granted, False otherwise.
    If the page cannot be loaded, assume no permission.
    """
    print("[5/5] Checking bot control permission...")
    try:
        req = urllib.request.Request(BOT_CONTROL_URL, headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://opengeofiction.net/",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        print(f"  WARNING: Could not load bot control page: {e}")
        return False

    if "{{permission|yes}}" in raw:
        print("  ✓ Bot permission granted")
        return True
    else:
        print("  ✗ Bot permission NOT granted (missing {{permission|yes}})")
        return False

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
        # Skip territories with no status entry (timezone boundaries, etc.) -
        # these are not real territories in the admin sense.
        status_info = statuses.get(ogf_id, {"status": "unknown", "owner": None})
        if status_info["status"] == "outline":
            continue
        if status_info["status"] == "unknown":
            continue
        if point_in_polygon_with_holes(lon, lat, terr["outer_ring"], terr["holes"]):
            terr_type = "permissible" if ogf_id in permissible else "restricted"
            violations.append((ogf_id, status_info["status"], status_info["owner"], terr_type))
    return violations

def patrol_user(username, user_id, territories, permissible, statuses, territory_version, notified_users=None):
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

        # Check for cached violation results for this changeset + territory version
        cached = get_cached_violations(cs["id"], territory_version)
        if cached is not None:
            cs_violations, cs_territories, nodes_count = cached
            report["violations"].extend(cs_violations)
            report["territories_mapped"].update(cs_territories)
            report["nodes_checked"] += nodes_count
            continue

        nodes = fetch_changeset_nodes(cs["id"], user_id)
        report["nodes_checked"] += len(nodes)

        cs_violations = []
        cs_territories = set()

        for node in nodes:
            hits = check_node_against_territories(
                node["lon"], node["lat"],
                territories, permissible, statuses
            )

            if not hits:
                # Node is not inside any territory polygon (e.g., in the sea).
                # Check if it's within the boundary buffer of any real territory
                # (not unknown/outline) before flagging it as a violation.
                near_boundary = False
                for ogf_id, terr in territories.items():
                    if not terr["outer_ring"]:
                        continue
                    # Skip unknown territories (timezone boundaries, etc.) and outlines
                    si = statuses.get(ogf_id, {"status": "unknown"})
                    if si["status"] in ("unknown", "outline"):
                        continue
                    if point_near_polygon(
                        node["lon"], node["lat"],
                        terr["outer_ring"],
                        TERRITORY_BUFFER_DEG,
                    ):
                        near_boundary = True
                        break
                    if terr.get("holes"):
                        for hole in terr["holes"]:
                            if point_near_polygon(
                                node["lon"], node["lat"],
                                hole,
                                TERRITORY_BUFFER_DEG,
                            ):
                                near_boundary = True
                                break
                        if near_boundary:
                            break
                if near_boundary:
                    continue
                # Node is not inside any territory polygon (e.g., in the sea)
                v = {
                    "changeset_id": cs["id"],
                    "node_id": node["id"],
                    "lat": node["lat"],
                    "lon": node["lon"],
                    "territory_id": None,
                    "territory_status": "outside territory",
                    "territory_owner": None,
                    "node_tags": node.get("tags", {}),
                }
                cs_violations.append(v)
                report["violations"].append(v)
                continue

            for terr_id, status, owner, terr_type in hits:
                cs_territories.add(terr_id)
                report["territories_mapped"].add(terr_id)

                if terr_type == "restricted":
                    v = {
                        "changeset_id": cs["id"],
                        "node_id": node["id"],
                        "lat": node["lat"],
                        "lon": node["lon"],
                        "territory_id": terr_id,
                        "territory_status": status,
                        "territory_owner": owner,
                        "node_tags": node.get("tags", {}),
                    }
                    cs_violations.append(v)
                    report["violations"].append(v)

        # Cache the violation results for this changeset
        cache_violations(cs["id"], territory_version, cs_violations, cs_territories, len(nodes))

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
            if status == "outside territory":
                reasons.append(f"Mapped {count} nodes outside any territory")
                score -= 2
            elif status == "reserved" or status == "archived":
                reasons.append(f"Mapped {count} nodes in {status} territories")
                score -= 3
            elif status == "owned" or status == "available" or status == "marked for withdrawal":
                suffix = " (needs admin approval)" if status == "available" else ""
                reasons.append(f"Mapped {count} nodes in {status} territories{suffix}")
                score -= 2
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

# ─── Notification Messaging ──────────────────────────────────────────────────

NOTIFICATION_TEMPLATE = """Hi there, I just noticed your [recent mapping]({changeset_url}) in OpenGeofiction.

Unfortunately, this area is not open to public editing. Before making any more edits here, please take a moment to read the [getting started](https://wiki.opengeofiction.net/index.php/OpenGeofiction:Getting_started) and [site policies pages,](https://wiki.opengeofiction.net/index.php/OpenGeofiction:Site_policies) which have instructions for new users.

Please note that new users can edit only in the blue territories on the [overview map.](http://wiki.opengeofiction.net/index.php/OpenGeofiction:Territories) Once you've built up a lengthier edit history, you will be able to [request a territory](https://wiki.opengeofiction.net/index.php/OpenGeofiction:Territory_assignment). You may also be interested in participating in a [collaborative project](https://wiki.opengeofiction.net/index.php/OpenGeofiction:List_of_collaborative_projects).

Thanks/brothie (Adminbot, for the admin team) -- THIS ACCOUNT IS NOT MONITORED, DO NOT REPLY"""

NOTIFICATION_TEMPLATE_SEA = """Hi there, I just noticed your [recent mapping]({changeset_url}) in OpenGeofiction.

Unfortunately, you are mapping outside any territory. Before making any more edits, please take a moment to read the [getting started](https://wiki.opengeofiction.net/index.php/OpenGeofiction:Getting_started) and [site policies pages,](https://wiki.opengeofiction.net/index.php/OpenGeofiction:Site_policies) which have instructions for new users.

Please note that new users can edit only in the blue territories on the [overview map.](http://wiki.opengeofiction.net/index.php/OpenGeofiction:Territories) Once you've built up a lengthier edit history, you will be able to [request a territory](https://wiki.opengeofiction.net/index.php/OpenGeofiction:Territory_assignment). You may also be interested in participating in a [collaborative project](https://wiki.opengeofiction.net/index.php/OpenGeofiction:List_of_collaborative_projects).

Thanks/brothie (Adminbot, for the admin team) -- THIS ACCOUNT IS NOT MONITORED, DO NOT REPLY"""

def get_most_recent_violation_changeset(report):
    """Get the most recent violating changeset URL from a patrol report."""
    if not report["violations"]:
        return None
    # Violations are in order they were found; get the last one (most recent)
    last_violation = report["violations"][-1]
    changeset_id = last_violation.get("changeset_id")
    if changeset_id:
        return f"https://opengeofiction.net/changeset/{changeset_id}"
    return None

def send_notification_to_user(session_cookie, username, report, dry_run=False):
    """
    Send a notification message to a user about territorial violations.
    
    Args:
        session_cookie: OGF session cookie
        username: The username to notify
        report: The patrol report for this user
        dry_run: If True, only print what would be sent
    
    Returns:
        True if message was sent successfully, False otherwise.
    """
    changeset_url = get_most_recent_violation_changeset(report)
    if not changeset_url:
        print(f"  [NOTIFY ERROR] No violating changeset found for {username}")
        return False
    
    # Pick template based on violation types
    has_sea_only = all(v.get("territory_status") == "outside territory" for v in report["violations"])
    body = NOTIFICATION_TEMPLATE_SEA.format(changeset_url=changeset_url) if has_sea_only \
        else NOTIFICATION_TEMPLATE.format(changeset_url=changeset_url)
    
    subject = "Welcome to OpenGeofiction - Important mapping notice"
    
    if dry_run:
        print(f"  [DRY-RUN] Would send message to {username}:")
        print(f"    Subject: {subject}")
        print(f"    Changeset: {changeset_url}")
        return True
    
    print(f"  [NOTIFY] Sending message to {username}...", end=" ", flush=True)
    success = send_ogf_message(session_cookie, username, subject, body, "Brothie")
    if success:
        print("✓ Sent")
    else:
        print("✗ Failed")
    return success

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

        CREATE TABLE IF NOT EXISTS changeset_violations (
            changeset_id INTEGER NOT NULL,
            territory_version TEXT NOT NULL,
            violations_json TEXT NOT NULL,
            territories_mapped_json TEXT NOT NULL,
            nodes_count INTEGER NOT NULL,
            cached_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (changeset_id, territory_version)
        );
        CREATE INDEX IF NOT EXISTS idx_violations_version ON changeset_violations(territory_version);
    """)
    conn.commit()

    # Clean up old cache entries (older than 60 days)
    c.execute("DELETE FROM changeset_cache WHERE fetched_at < datetime('now', '-60 days')")
    deleted_nodes = c.rowcount
    c.execute("DELETE FROM changeset_violations WHERE cached_at < datetime('now', '-60 days')")
    deleted_violations = c.rowcount
    conn.commit()
    deleted = deleted_nodes + deleted_violations
    if deleted > 0:
        print(f"  [CACHE] Cleaned up {deleted} old cache entries ({deleted_nodes} node + {deleted_violations} violation)")

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

CACHE_LOGIC_VERSION = "v2"  # Bump when violation detection logic changes (invalidates cache)

def compute_territory_version(territories, statuses, permissible):
    """Compute a stable hash of territory data for cache invalidation.

    Returns a short hex string that changes when any polygon shape,
    hole, status, permissible flag, or detection logic changes.
    """
    data = {
        "version": CACHE_LOGIC_VERSION,
        "territories": {
            k: {
                "outer_len": len(v["outer_ring"]),
                "holes": [len(h) for h in v.get("holes", [])],
            }
            for k, v in sorted(territories.items())
        },
        "statuses": {k: v for k, v in sorted(statuses.items())},
        "permissible": sorted(permissible),
        "buffer": TERRITORY_BUFFER_DEG,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

def get_cached_violations(changeset_id, territory_version):
    """Retrieve cached violation results for a changeset.

    Returns (violations_list, territories_mapped_set, nodes_count) or None.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT violations_json, territories_mapped_json, nodes_count FROM changeset_violations WHERE changeset_id = ? AND territory_version = ?",
        (changeset_id, territory_version)
    )
    row = c.fetchone()
    conn.close()
    if row:
        return json.loads(row[0]), set(json.loads(row[1])), row[2]
    return None

def cache_violations(changeset_id, territory_version, violations, territories_mapped, nodes_count):
    """Store violation results for a changeset."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO changeset_violations (changeset_id, territory_version, violations_json, territories_mapped_json, nodes_count, cached_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (changeset_id, territory_version, json.dumps(violations), json.dumps(list(territories_mapped)), nodes_count)
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
            "latest": u_info.get("latest", ""),
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
    Output fields: name, profile, block_status, classification, latest, violations, notified, notes
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
            "latest": u_info.get("latest", ""),
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
    dry_run = False
    send_notifications = False
    
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
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        elif args[i] == "--notify":
            send_notifications = True
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
    permitted = check_bot_permission()
    statuses_global = statuses  # For use in classify_user
    territory_version = compute_territory_version(territories, statuses, permissible)
    print(f"  Permissible (blue) territories: {len(permissible)}")
    print(f"  Territory version: {territory_version}")

    # Gate --notify and --scp behind bot control permission
    if not permitted:
        if send_notifications:
            print("  ⛔ Bot permission denied — skipping --notify")
            send_notifications = False
        if scp_target:
            print("  ⛔ Bot permission denied — skipping --scp")
            scp_target = None

    all_reports = []
    all_classifications = []
    
    # Filter to target user if specified
    if target_user is not None:
        users = [u for u in users if u["id"] == target_user]
    
    for i, user in enumerate(users):
        print(f"\n[{i+1}/{len(users)}] Patrolling {user['name']} (ID: {user['id']})...", flush=True)
        report = patrol_user(user["name"], user["id"], territories, permissible, statuses, territory_version, notified_users)
        all_reports.append(report)
        classification = classify_user(user, report)
        all_classifications.append(classification)
        print_report(report, classification)
    
    print_summary(all_reports, all_classifications)
    
    # ─── Send Notifications ──────────────────────────────────────────────────
    
    if send_notifications:
        print(f"\n{'='*60}")
        if dry_run:
            print(f"  DRY-RUN MODE - No messages will be sent")
        print(f"  SENDING NOTIFICATIONS")
        print(f"{'='*60}")
        
        # Load credentials
        credentials = load_ogf_credentials()
        if not credentials.get("USERNAME") or not credentials.get("PASSWORD"):
            print(f"  [ERROR] Could not load credentials from {CREDENTIALS_PATH}")
        else:
            username = credentials["USERNAME"]
            password = credentials["PASSWORD"]
            print(f"  Logging in as {username}...")
            
            # Login to OGF for messaging
            session_cookie, _ = ogf_login(username, password)
            
            # Login to wiki for editing (may fail due to bot protection)
            wiki_cookie_jar, wiki_opener = wiki_login(username, password)
            
            if not session_cookie:
                print(f"  [ERROR] Failed to login to OGF for messaging")
            else:
                print(f"  ✓ Logged in to OGF")
                
                if not wiki_opener:
                    print(f"  [WARN] Wiki login failed - wiki updates will be skipped")
                    print(f"         Please manually add notified users to: https://wiki.opengeofiction.net/index.php/Help:New_user_patrol")
                
                # Track users to notify and successfully notified users
                users_to_notify = []
                successfully_notified = []
                
                for report, cls, user_info in zip(all_reports, all_classifications, users):
                    # Check if user should be notified:
                    # 1. Classification is needs_review or worse
                    # 2. Has violations
                    # 3. Not already in notified_users list
                    should_notify = (
                        cls["classification"] in ("needs_review", "suspicious", "likely_vandal") and
                        len(report["violations"]) > 0 and
                        report["username"] not in notified_users
                    )
                    
                    if should_notify:
                        users_to_notify.append((report, cls, user_info))
                
                if not users_to_notify:
                    print(f"  No users require notification")
                else:
                    print(f"  Found {len(users_to_notify)} user(s) to notify")
                    
                    for report, cls, user_info in users_to_notify:
                        username_to_notify = report["username"]
                        
                        # Send message
                        success = send_notification_to_user(session_cookie, username_to_notify, report, dry_run)
                        
                        if success:
                            # Message was sent successfully - track it
                            successfully_notified.append(username_to_notify)
                            
                            # Add to wiki contacted list (unless dry-run)
                            if not dry_run and wiki_opener:
                                print(f"  [WIKI] Adding {username_to_notify} to contacted users list... ", end="", flush=True)
                                wiki_session = (wiki_cookie_jar, wiki_opener)
                                wiki_success = add_contacted_user_wiki(wiki_session, username_to_notify, "wangi")
                                if wiki_success:
                                    print("✓ Added")
                                else:
                                    print("✗ Failed (falling back to local DB)")
                            elif dry_run:
                                print(f"  [DRY-RUN] Would add {username_to_notify} to wiki contacted list")
                            else:
                                print(f"  [SKIP] Wiki update skipped (wiki login unavailable)")
                        
                        # Small delay between messages to be respectful
                        if not dry_run:
                            time.sleep(1)
                
                print(f"\n  Notification summary: {len(successfully_notified)} user(s) notified")
                if successfully_notified:
                    print(f"    Notified: {', '.join(successfully_notified)}")
    
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
