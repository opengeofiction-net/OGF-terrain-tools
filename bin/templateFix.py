#!/usr/bin/env python3
"""
templateFix.py - Pick a random territory application page and replace bare IDs
and URLs with wiki templates.

Run on a cron schedule to gradually clean up territory application pages.

Patterns replaced:
  - Bare territory IDs (AN106, UL07f) → {{relation|rel_id|territory_id}}
  - Map URLs (opengeofiction.net/#map=Z/LAT/LON...) → {{coord|lat|lon|zoom=Z}}
  - Way URLs → {{way|id}}
  - Relation URLs → {{relation|id}}
  - Node URLs → {{node|id}}
  - Changeset URLs → {{changeset|id}}

Credentials: ~/ogf-user.env (USERNAME, PASSWORD)
"""

import json
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WIKI_API_URL = "https://wiki.opengeofiction.net/api.php"
TERRITORY_LOOKUP_URL = (
    "https://wiki.opengeofiction.net/index.php"
    "/OpenGeofiction:Territory_administration?action=raw"
)
BOT_CONTROL_URL = "https://wiki.opengeofiction.net/index.php/User:Brothie?action=raw"
USER_AGENT = "Brothie/1.0 (OGF Template Bot)"
REFERER = "https://opengeofiction.net/"
CREDENTIALS_PATH = Path.home() / "ogf-user.env"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_credentials():
    """Read USERNAME and PASSWORD from ~/ogf-user.env."""
    creds = {}
    with open(CREDENTIALS_PATH) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                creds[key.strip()] = value.strip().strip('"').strip("'")
    return creds


def _build_request(url, data=None):
    """Create a urllib Request with the required headers."""
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Referer", REFERER)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return req


def api_get(opener, params):
    """GET from the MediaWiki API. Returns parsed JSON or None."""
    url = f"{WIKI_API_URL}?{urllib.parse.urlencode(params)}"
    req = _build_request(url)
    try:
        resp = opener.open(req)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"  HTTP Error {exc.code}: {exc.reason}")
        return None


def api_post(opener, params):
    """POST to the MediaWiki API. Returns parsed JSON or None."""
    data = urllib.parse.urlencode(params).encode()
    req = _build_request(WIKI_API_URL, data)
    try:
        resp = opener.open(req)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"  HTTP Error {exc.code}: {exc.reason}")
        return None


# ---------------------------------------------------------------------------
# Wiki operations
# ---------------------------------------------------------------------------
def login(opener, username, password):
    """Authenticate via MediaWiki API clientlogin."""
    token_data = api_get(opener, {
        "action": "query", "meta": "tokens",
        "type": "login", "format": "json",
    })
    if not token_data:
        return False
    login_token = token_data["query"]["tokens"]["logintoken"]

    result = api_post(opener, {
        "action": "clientlogin",
        "loginreturnurl": WIKI_API_URL,
        "logintoken": login_token,
        "username": username,
        "password": password,
        "format": "json",
    })
    if result:
        status = result.get("clientlogin", {}).get("status", "")
        return status == "PASS"
    return False


def get_category_pages(opener):
    """Return list of page titles in Category:Territory_application (pages only)."""
    data = api_get(opener, {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": "Category:Territory application",
        "cmtype": "page",
        "cmlimit": "50",
        "format": "json",
    })
    if not data:
        return []
    return [m["title"] for m in data.get("query", {}).get("categorymembers", [])]


def get_page_content(opener, title):
    """Fetch wikitext content and pageid. Returns (content, pageid) or (None, None)."""
    data = api_get(opener, {
        "action": "query",
        "titles": title,
        "prop": "revisions",
        "rvprop": "content",
        "format": "json",
    })
    if not data:
        return None, None
    for pid, page in data.get("query", {}).get("pages", {}).items():
        if pid == "-1":
            return None, None
        revs = page.get("revisions", [])
        if revs:
            return revs[0].get("*", ""), page.get("pageid")
    return None, None


def edit_page(opener, title, text, summary, pageid):
    """Save wikitext via the API. Returns True on success."""
    token_data = api_get(opener, {
        "action": "query", "meta": "tokens",
        "type": "csrf", "format": "json",
    })
    if not token_data:
        return False
    csrf = token_data["query"]["tokens"]["csrftoken"]

    result = api_post(opener, {
        "action": "edit",
        "title": title,
        "text": text,
        "summary": summary,
        "token": csrf,
        "bot": "1",
        "format": "json",
    })
    if result:
        return result.get("edit", {}).get("result") == "Success"
    return False


# ---------------------------------------------------------------------------
# Territory lookup
# ---------------------------------------------------------------------------
def load_territory_lookup():
    """Fetch territory → relation ID mapping from the wiki admin page."""
    req = _build_request(TERRITORY_LOOKUP_URL)
    try:
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read().decode())
        return {item["ogfId"]: item["rel"] for item in data}
    except Exception as exc:
        print(f"Failed to load territory lookup: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Wikitext transformation
# ---------------------------------------------------------------------------
def transform_wikitext(content, territory_map):
    """Apply all template replacements. Returns (new_content, list_of_change_descriptions)."""
    changes = []

    # ---- Pass 1: Replace opengeofiction.net object/map URLs ------------
    # Map URL:  #map=Z/LAT/LON[&...]
    # Order matters: object URLs first so their numeric IDs aren't consumed
    # by the broader map-URL pattern.

    # Object URLs inside wikilinks: [https://.../way/ID text] → {{way|ID|name=text}}
    # Must handle before bare URLs so the [ ] don't get stripped incompletely.
    for obj_type, tmpl in [
        ("changeset", "changeset"),
        ("way", "way"),
        ("relation", "relation"),
        ("node", "node"),
    ]:
        # Wikilink form: [URL text]
        wikilink_pat = re.compile(
            r"\[https?://(?:www\.)?opengeofiction\.net/"
            + obj_type
            + r"/(\d+)(?:[?#][^\s\]]*)?\s+"
            r"([^\]]+)\]"
        )

        def _obj_wikilink_repl(m, _tmpl=tmpl, _obj_type=obj_type):
            oid = m.group(1)
            name = m.group(2).strip()
            # Changeset template has no second param; others use unnamed param 2 for display text
            if _tmpl == "changeset":
                changes.append(f"{_obj_type} wikilink {oid} → {{{{{_tmpl}|{oid}}}}}")
                return f"{{{{{_tmpl}|{oid}}}}}"
            changes.append(f"{_obj_type} wikilink {oid} → {{{{{_tmpl}|{oid}|{name}}}}}")
            return f"{{{{{_tmpl}|{oid}|{name}}}}}"

        content = wikilink_pat.sub(_obj_wikilink_repl, content)

        # Bare URL form (not inside [...])
        bare_pat = re.compile(
            r"https?://(?:www\.)?opengeofiction\.net/"
            + obj_type
            + r"/(\d+)(?:[?#][^\s\]<>]*)?"
        )

        def _obj_repl(m, _tmpl=tmpl, _obj_type=obj_type):
            oid = m.group(1)
            changes.append(f"{_obj_type} {oid} → {{{{{_tmpl}|{oid}}}}}")
            return f"{{{{{_tmpl}|{oid}}}}}"

        content = bare_pat.sub(_obj_repl, content)

    # Coord / map URLs:  #map=zoom/lat/lon[&...]
    # Two patterns: one for wikilinks [URL text], one for bare URLs.
    # Handle wikilinks first so the outer brackets don't interfere.
    coord_wikilink_pat = re.compile(
        r"\[https?://(?:www\.)?opengeofiction\.net/#map="
        r"(\d+)/([-+]?\d+(?:\.\d+)?)/([-+]?\d+(?:\.\d+)?)"
        r"(?:[&?][^\s\]]*)?\s+"
        r"([^\]]+)\]"
    )

    def _coord_wikilink_repl(m):
        zoom, lat, lon, name = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        changes.append(f"map link → {{coord|latitude={lat}|longitude={lon}|zoom={zoom}|name={name}}}")
        return f"{{{{coord|latitude={lat}|longitude={lon}|zoom={zoom}|name={name}}}}}"

    content = coord_wikilink_pat.sub(_coord_wikilink_repl, content)

    # Standalone coord URLs (not inside [])
    coord_pat = re.compile(
        r"https?://(?:www\.)?opengeofiction\.net/#map="
        r"(\d+)/([-+]?\d+(?:\.\d+)?)/([-+]?\d+(?:\.\d+)?)"
        r"(?:[&?][^\s\]<>]*)?"
    )

    def _coord_repl(m):
        zoom, lat, lon = m.group(1), m.group(2), m.group(3)
        changes.append(f"map link → {{coord|latitude={lat}|longitude={lon}|zoom={zoom}}}")
        return f"{{{{coord|latitude={lat}|longitude={lon}|zoom={zoom}}}}}"

    content = coord_pat.sub(_coord_repl, content)

    # ---- Pass 2: Replace bare territory IDs (first occurrence only) ----
    # Build alternation of all known territory IDs, longest first to avoid
    # partial matches (e.g. AN106 matching inside AN106a).
    sorted_ids = sorted(territory_map.keys(), key=len, reverse=True)
    # Escape any regex-special chars in IDs (none expected, but safe).
    escaped = [re.escape(tid) for tid in sorted_ids]
    id_pat = "|".join(escaped)

    # Match territory IDs while skipping content inside {{...}} templates
    # (so already-templated references like {{relation|314512|AN106}} are left alone).
    # Only the FIRST occurrence of each territory ID is replaced.
    seen_ids = set()
    territory_re = re.compile(
        r"{{[^}]*}}"   # skip template spans
        r"|"
        rf"\b({id_pat})\b"  # capture territory IDs
    )

    def _tid_repl(m):
        if m.group(0).startswith("{{"):
            return m.group(0)  # leave existing templates untouched
        tid = m.group(1)
        if tid in seen_ids:
            return m.group(0)  # only replace first occurrence
        seen_ids.add(tid)
        rid = territory_map[tid]
        changes.append(f"territory ID {tid} → {{{{relation|{rid}|{tid}}}}}")
        return f"{{{{relation|{rid}|{tid}}}}}"

    content = territory_re.sub(_tid_repl, content)

    return content, changes


# --------------------------------------------------------------------------
# Bot permission check
# --------------------------------------------------------------------------
def check_bot_permission():
    """
    Check the bot control wiki page for {{permission|yes}}.
    Returns True if permission is granted, False otherwise.
    If the page cannot be loaded, assume no permission.
    """
    try:
        req = urllib.request.Request(BOT_CONTROL_URL, headers={
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
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


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv

    creds = load_credentials()
    username = creds.get("USERNAME", "")
    password = creds.get("PASSWORD", "")
    if not username or not password:
        print("Error: No credentials in ~/ogf-user.env")
        sys.exit(1)

    territory_map = load_territory_lookup()
    if not territory_map:
        print("Error: Failed to load territory lookup")
        sys.exit(1)
    print(f"Loaded {len(territory_map)} territory IDs")

    # Set up session with cookie jar
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj)
    )

    if not login(opener, username, password):
        print("Error: Login failed")
        sys.exit(1)
    print("Logged in to wiki")

    if not check_bot_permission():
        print("Bot permission denied, exiting")
        sys.exit(1)

    pages = get_category_pages(opener)
    if not pages:
        print("Error: No pages found in Category:Territory application")
        sys.exit(1)
    print(f"Found {len(pages)} pages in category")

    title = random.choice(pages)
    print(f"Selected: {title}")

    content, pageid = get_page_content(opener, title)
    if content is None:
        print(f"Error: Could not fetch content for {title}")
        sys.exit(1)
    print(f"Page ID {pageid}, {len(content)} characters")

    new_content, changes = transform_wikitext(content, territory_map)

    if not changes:
        print("No replacements needed — exiting")
        sys.exit(0)

    print(f"\n{len(changes)} replacement(s) to apply:")
    for c in changes:
        print(f"  {c}")

    if dry_run:
        print("\nDry-run mode — no edit saved")
        sys.exit(0)

    if edit_page(opener, title, new_content,
                 "Replace bare territory IDs and map URLs with wiki templates",
                 pageid):
        print(f"\nSuccessfully updated {title}")
    else:
        print(f"\nFailed to save edit for {title}")
        sys.exit(1)


if __name__ == "__main__":
    main()
