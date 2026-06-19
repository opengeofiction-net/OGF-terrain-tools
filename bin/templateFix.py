#!/usr/bin/env python3
"""
templateFix.py - Pick a random territory application page and replace bare IDs
and URLs with wiki templates.

Run on a cron schedule to gradually clean up territory application pages.

Patterns replaced:
  - Bare territory IDs (AN106, UL07f) → {{relation|rel_id|territory_id}}  (first occurrence only)
  - OGF map URLs → {{coord|latitude=|longitude=|zoom=}}
  - OGF edit URLs (/edit#map=...) → {{coord|latitude=|longitude=|zoom=}}
  - OSM map URLs → {{coordosm|latitude=|longitude=|zoom=}}
  - OGF way/relation/node/changeset URLs → respective templates
  - OGF user profile URLs → {{OGF user|username}} or {{OGF user|username|history}}
  - OGF message/new URLs → {{OGF user|username|msg}} or {{OGF user|username|msg|text=...}}
  - Wikilink forms [URL display_text] preserve display text in template params
  - {{#multimaps:...}} blocks are protected — URLs inside them are never modified

Credentials: ~/ogf-user.env (USERNAME, PASSWORD)
"""

import datetime
import json
import random
import re
import sys
import time
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
VAR_DIR = Path(__file__).parent.parent / "var"


def write_daily_book(entry):
    """Append a JSONL entry to the daily book for today's date."""
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    path = VAR_DIR / f"daily-book-{today}.ndjson"
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


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


def get_random_page(opener):
    """Pick a random wiki page, excluding Template, Admin, OpenGeofiction, and Help namespaces."""
    # Allowed namespaces: main (0), Talk (1), User (2), User talk (3), File (6),
    # File talk (7), MediaWiki (8), Category (14), Forum (110), Collab (3002), Index (3004)
    # Excluded: Template (10), OpenGeofiction (4), Help (12), Admin (3006), and their talk pages
    allowed_ns = "0|1|2|3|6|7|8|14|110|3002|3004"
    data = api_get(opener, {
        "action": "query",
        "list": "random",
        "rnnamespace": allowed_ns,
        "rnlimit": "1",
        "format": "json",
    })
    if not data:
        return None
    pages = data.get("query", {}).get("random", [])
    return pages[0]["title"] if pages else None


def get_recently_changed_pages(opener, hours=24):
    """Fetch pages changed in the last N hours via the recentchanges API.

    Returns a list of unique page titles, excluding Template:,
    OpenGeofiction:, User:Brothie (including subpages), and pages
    where Brothie was the last editor (the bot already cleaned them).
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    cutoff_ts = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    pages = set()
    rcstart = None  # start from the most recent

    while True:
        params = {
            "action": "query",
            "list": "recentchanges",
            "rcprop": "title|timestamp|user",
            "rclimit": "max",        # 500 per request
            "rctype": "edit|new",
            "rctoponly": "1",         # one entry per page
            "format": "json",
        }
        if rcstart:
            params["rcstart"] = rcstart

        data = api_get(opener, params)
        if not data:
            break

        changes = data.get("query", {}).get("recentchanges", [])
        done = False
        for change in changes:
            if change["timestamp"] < cutoff_ts:
                done = True
                break
            title = change["title"]
            # Excluded namespaces and pages
            if title.startswith("Template:") or title.startswith("OpenGeofiction:"):
                continue
            if title.startswith("Admin:"):
                continue
            if title == "User:Brothie" or title.startswith("User:Brothie/"):
                continue
            if title == "Help:Frequently asked questions":
                continue
            if title == "Main Page" or title.startswith("Main Page/"):
                continue
            # Skip pages where the bot itself was the last editor
            if change.get("user") == "Brothie":
                continue
            pages.add(title)

        if done or "continue" not in data:
            break
        rcstart = data["continue"]["rccontinue"]

    return sorted(pages)


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
    if "error" in data:
        print(f"  API error: {data['error'].get('code')} — {data['error'].get('info')}")
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

    # ---- Pass 0: Protect {{#multimaps:...}} blocks from processing ----
    # These parser functions contain embedded HTML with OGF URLs that must
    # NOT be templated — replacing them breaks the multimaps feature.
    protected = {}

    def _protect_multimaps(text):
        """Replace {{#multimaps:...}} spans with placeholders."""
        nonlocal protected
        protected = {}
        result = []
        i = 0
        placeholder_idx = 0
        while i < len(text):
            # Find next {{#multimaps:
            mm_pos = text.find("{{#multimaps:", i)
            if mm_pos == -1:
                result.append(text[i:])
                break
            result.append(text[i:mm_pos])
            # Count braces to find matching }}
            brace_depth = 0
            j = mm_pos
            while j < len(text):
                if text[j:j+2] == "{{":
                    brace_depth += 1
                    j += 1
                elif text[j:j+2] == "}}":
                    brace_depth -= 1
                    if brace_depth == 0:
                        full_block = text[mm_pos:j+2]
                        ph = f"__MMPROTECT_{placeholder_idx}__"
                        protected[ph] = full_block
                        result.append(ph)
                        placeholder_idx += 1
                        j += 2
                        break
                    j += 1
                else:
                    j += 1
            else:
                # Unclosed — append rest as-is to avoid data loss
                result.append(text[mm_pos:])
                break
            i = j
        return "".join(result)

    content = _protect_multimaps(content)

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

    # OGF user profile URLs: opengeofiction.net/user/NAME[/history]
    # {{OGF user|NAME}} or {{OGF user|NAME|history}}
    # Username may contain spaces, %20, or plus signs.

    # Wikilink form first: [https://.../user/NAME(/history)? TEXT] → {{OGF user|NAME}}
    # Display text is dropped since the template generates it from the username.
    user_wikilink_history_pat = re.compile(
        r"\[https?://(?:www\.)?opengeofiction\.net/user/([^/]+?)/history"
        r"(?:[?#][^\s\]]*)?\s+"
        r"[^\]]*\]"
    )
    user_wikilink_pat = re.compile(
        r"\[https?://(?:www\.)?opengeofiction\.net/user/([^/>?\s#]+)"
        r"(?:[?#][^\s\]]*)?\s+"
        r"[^\]]*\]"
    )

    # Diary URLs (/user/NAME/diary/NNN) — no template exists, leave untouched.
    # Use placeholders to protect them from the user-profile patterns below.
    diary_pat = re.compile(
        r"(?:\[)?"                                  # optional wikilink bracket
        r"https?://(?:www\.)?opengeofiction\.net/user/[^/\s]+/diary/\d+"
        r"(?:[?#][^\s\]<>]*)?"
        r"(?:\s+[^\]]*\])?"                         # optional wikilink display text + close
    )
    diary_protected = {}

    def _protect_diary(m):
        nonlocal diary_protected
        idx = len(diary_protected)
        ph = f"__DIARYPROTECT_{idx}__"
        diary_protected[ph] = m.group(0)
        return ph

    content = diary_pat.sub(_protect_diary, content)

    def _user_wikilink_repl(m, history=False):
        # Decode %20 and + to spaces for the template parameter
        raw_name = m.group(1)
        name = raw_name.replace("%20", " ").replace("+", " ")
        if history:
            changes.append(f"user wikilink {raw_name}/history → {{{{OGF user|{name}|history}}}}")
            return f"{{{{OGF user|{name}|history}}}}"
        changes.append(f"user wikilink {raw_name} → {{{{OGF user|{name}}}}}")
        return f"{{{{OGF user|{name}}}}}"

    # /history wikilinks must be matched first (before plain ones consume the url)
    content = user_wikilink_history_pat.sub(
        lambda m: _user_wikilink_repl(m, history=True), content
    )
    content = user_wikilink_pat.sub(
        lambda m: _user_wikilink_repl(m, history=False), content
    )

    # Bare URL form (not inside [...])
    user_bare_history_pat = re.compile(
        r"https?://(?:www\.)?opengeofiction\.net/user/([^/?\s]+)/history(?:[?#][^\s\]<>]*)?"
    )
    user_bare_pat = re.compile(
        r"https?://(?:www\.)?opengeofiction\.net/user/([^/?\s#]+)(?:[?#][^\s\]<>]*)?"
    )

    def _user_bare_repl(m, history=False):
        raw_name = m.group(1)
        name = raw_name.replace("%20", " ").replace("+", " ")
        if history:
            changes.append(f"user URL {raw_name}/history → {{{{OGF user|{name}|history}}}}")
            return f"{{{{OGF user|{name}|history}}}}"
        changes.append(f"user URL {raw_name} → {{{{OGF user|{name}}}}}")
        return f"{{{{OGF user|{name}}}}}"

    content = user_bare_history_pat.sub(
        lambda m: _user_bare_repl(m, history=True), content
    )
    content = user_bare_pat.sub(
        lambda m: _user_bare_repl(m, history=False), content
    )

    # OGF message/new URLs: opengeofiction.net/message/new/NAME
    # {{OGF user|NAME|msg}} or {{OGF user|NAME|msg|text=display text}}
    # Username may contain URL-encoded characters (%20, +, etc.)

    # Wikilink form first: [https://.../message/new/NAME TEXT]
    msg_wikilink_pat = re.compile(
        r"\[https?://(?:www\.)?opengeofiction\.net/message/new/"
        r"([^\]/\s?#]+)"
        r"(?:[?#][^\s\]]*)?\s+"
        r"([^\]]+)\]"
    )

    def _msg_wikilink_repl(m):
        raw_name = m.group(1)
        name = raw_name.replace("%20", " ").replace("+", " ")
        display_text = m.group(2).strip()
        changes.append(f"msg wikilink {raw_name} → {{{{OGF user|{name}|msg|text={display_text}}}}}")
        return f"{{{{OGF user|{name}|msg|text={display_text}}}}}"

    content = msg_wikilink_pat.sub(_msg_wikilink_repl, content)

    # Bare URL form (not inside [...])
    msg_bare_pat = re.compile(
        r"https?://(?:www\.)?opengeofiction\.net/message/new/"
        r"([^\]/<\s?#]+)"
        r"(?:[?#][^\s\]<>]*)?"
    )

    def _msg_bare_repl(m):
        raw_name = m.group(1)
        name = raw_name.replace("%20", " ").replace("+", " ")
        changes.append(f"msg URL {raw_name} → {{{{OGF user|{name}|msg}}}}")
        return f"{{{{OGF user|{name}|msg}}}}"

    content = msg_bare_pat.sub(_msg_bare_repl, content)

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

    # ---- Pass 1a (history): OGF /history#map= URLs → {{coord|action=history}} ----
    # History links open the edit history view at the given coordinates.

    history_wikilink_pat = re.compile(
        r"\[https?://(?:www\.)?opengeofiction\.net/history#map="
        r"(\d+)/([-+]?\d+(?:\.\d+)?)/([-+]?\d+(?:\.\d+)?)"
        r"(?:[&?][^\s\]]*)?\s+"
        r"([^\]]+)\]"
    )

    def _history_wikilink_repl(m):
        zoom, lat, lon, name = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        changes.append(f"history link → {{coord|latitude={lat}|longitude={lon}|zoom={zoom}|action=history|name={name}}}")
        return f"{{{{coord|latitude={lat}|longitude={lon}|zoom={zoom}|action=history|name={name}}}}}"

    content = history_wikilink_pat.sub(_history_wikilink_repl, content)

    history_bare_pat = re.compile(
        r"https?://(?:www\.)?opengeofiction\.net/history#map="
        r"(\d+)/([-+]?\d+(?:\.\d+)?)/([-+]?\d+(?:\.\d+)?)"
        r"(?:[&?][^\s\]<>]*)?"
    )

    def _history_bare_repl(m):
        zoom, lat, lon = m.group(1), m.group(2), m.group(3)
        changes.append(f"history link → {{coord|latitude={lat}|longitude={lon}|zoom={zoom}|action=history}}")
        return f"{{{{coord|latitude={lat}|longitude={lon}|zoom={zoom}|action=history}}}}"

    content = history_bare_pat.sub(_history_bare_repl, content)

    # ---- Pass 1a: Replace OGF edit URLs → {{coord}} ---------------------
    # Edit links like https://opengeofiction.net/edit#map=18/-11.86/170.58
    # carry coordinate fragments and should be converted to {{coord}}.
    # Wikilink form: [URL display text] → {{coord|...|name=display text}}
    # Bare form: URL → {{coord|...}}

    edit_wikilink_pat = re.compile(
        r"\[https?://(?:www\.)?opengeofiction\.net/edit#map="
        r"(\d+)/([-+]?\d+(?:\.\d+)?)/([-+]?\d+(?:\.\d+)?)"
        r"(?:[&?][^\s\]]*)?\s+"
        r"([^\]]+)\]"
    )

    def _edit_wikilink_repl(m):
        zoom, lat, lon, name = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        changes.append(f"edit link → {{coord|latitude={lat}|longitude={lon}|zoom={zoom}|name={name}}}")
        return f"{{{{coord|latitude={lat}|longitude={lon}|zoom={zoom}|name={name}}}}}"

    content = edit_wikilink_pat.sub(_edit_wikilink_repl, content)

    edit_bare_pat = re.compile(
        r"https?://(?:www\.)?opengeofiction\.net/edit#map="
        r"(\d+)/([-+]?\d+(?:\.\d+)?)/([-+]?\d+(?:\.\d+)?)"
        r"(?:[&?][^\s\]<>]*)?"
    )

    def _edit_bare_repl(m):
        zoom, lat, lon = m.group(1), m.group(2), m.group(3)
        changes.append(f"edit link → {{coord|latitude={lat}|longitude={lon}|zoom={zoom}}}")
        return f"{{{{coord|latitude={lat}|longitude={lon}|zoom={zoom}}}}}"

    content = edit_bare_pat.sub(_edit_bare_repl, content)

    # ---- Pass 1b: Replace openstreetmap.org URLs → {{coordosm}} ----------
    # OSM #map= coordinate links use {{coordosm}} (same params as {{coord}}).
    # OSM object URLs (way/relation/node/changeset) have no wiki templates
    # and are left as-is, EXCEPT when they also carry a #map= fragment —
    # those are converted to {{coordosm}} since the coordinates are the useful part.

    # OSM object+map wikilinks: [/relation/ID#map=Z/LAT/LON text] → {{coordosm}}
    # Must come before plain coordosm patterns so the object URL regex doesn't
    # consume these first. The object ID is silently dropped (no template for it).
    for obj_type in ("changeset", "way", "relation", "node"):
        osm_obj_map_wikilink = re.compile(
            r"\[https?://(?:www\.)?openstreetmap\.org/"
            + obj_type
            + r"/\d+#map="
            r"(\d+)/([-+]?\d+(?:\.\d+)?)/([-+]?\d+(?:\.\d+)?)"
            r"(?:[&?][^\s\]]*)?\s+"
            r"([^\]]+)\]"
        )

        def _osm_obj_map_repl(m):
            zoom, lat, lon, name = m.group(1), m.group(2), m.group(3), m.group(4).strip()
            changes.append(f"osm object+map link → {{coordosm|latitude={lat}|longitude={lon}|zoom={zoom}|name={name}}}")
            return f"{{{{coordosm|latitude={lat}|longitude={lon}|zoom={zoom}|name={name}}}}}"

        content = osm_obj_map_wikilink.sub(_osm_obj_map_repl, content)

        # Bare OSM object+map URLs (not inside []) → {{coordosm}} without name
        osm_obj_map_bare = re.compile(
            r"https?://(?:www\.)?openstreetmap\.org/"
            + obj_type
            + r"/\d+#map="
            r"(\d+)/([-+]?\d+(?:\.\d+)?)/([-+]?\d+(?:\.\d+)?)"
            r"(?:[&?][^\s\]<>]*)?"
        )

        def _osm_obj_map_bare_repl(m):
            zoom, lat, lon = m.group(1), m.group(2), m.group(3)
            changes.append(f"osm object+map link → {{coordosm|latitude={lat}|longitude={lon}|zoom={zoom}}}")
            return f"{{{{coordosm|latitude={lat}|longitude={lon}|zoom={zoom}}}}}"

        content = osm_obj_map_bare.sub(_osm_obj_map_bare_repl, content)

    # Pure OSM #map= wikilinks: [https://...openstreetmap.org/#map=Z/LAT/LON text]
    coordosm_wikilink_pat = re.compile(
        r"\[https?://(?:www\.)?openstreetmap\.org/#map="
        r"(\d+)/([-+]?\d+(?:\.\d+)?)/([-+]?\d+(?:\.\d+)?)"
        r"(?:[&?][^\s\]]*)?\s+"
        r"([^\]]+)\]"
    )

    def _coordosm_wikilink_repl(m):
        zoom, lat, lon, name = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        changes.append(f"osm map link → {{coordosm|latitude={lat}|longitude={lon}|zoom={zoom}|name={name}}}")
        return f"{{{{coordosm|latitude={lat}|longitude={lon}|zoom={zoom}|name={name}}}}}"

    content = coordosm_wikilink_pat.sub(_coordosm_wikilink_repl, content)

    # Standalone OSM coord URLs (not inside [])
    coordosm_pat = re.compile(
        r"https?://(?:www\.)?openstreetmap\.org/#map="
        r"(\d+)/([-+]?\d+(?:\.\d+)?)/([-+]?\d+(?:\.\d+)?)"
        r"(?:[&?][^\s\]<>]*)?"
    )

    def _coordosm_repl(m):
        zoom, lat, lon = m.group(1), m.group(2), m.group(3)
        changes.append(f"osm map link → {{coordosm|latitude={lat}|longitude={lon}|zoom={zoom}}}")
        return f"{{{{coordosm|latitude={lat}|longitude={lon}|zoom={zoom}}}}}"

    content = coordosm_pat.sub(_coordosm_repl, content)

    # ---- Pass 1c: OSM ?mlat=...&mlon=... URLs → {{coordosm}} -------------
    # These use query parameters instead of a #map= hash fragment.

    def _mlat_to_coordosm(url, name=""):
        """Convert an OSM ?mlat=&mlon= URL to {{coordosm}} template.
        Default zoom is 18 if not found in the URL.
        """
        mlat_m = re.search(r"mlat=([-+]?\d+(?:\.\d+)?)", url)
        mlon_m = re.search(r"mlon=([-+]?\d+(?:\.\d+)?)", url)
        lat = mlat_m.group(1) if mlat_m else ""
        lon = mlon_m.group(1) if mlon_m else ""
        zoom_m = re.search(r"#map=(\d+)", url)
        zoom = zoom_m.group(1) if zoom_m else "18"
        params = f"latitude={lat}|longitude={lon}|zoom={zoom}"
        if name:
            params += f"|name={name}"
        changes.append(f"osm mlat/mlon link → {{{{coordosm|{params}}}}}")
        return f"{{{{coordosm|{params}}}}}"

    # Wikilink form: [URL TEXT]
    mlat_wikilink_pat = re.compile(
        r"\[https?://(?:www\.)?openstreetmap\.org/\?"
        r"[^\s\[]+"
        r"\s+"
        r"([^\]]+)\]"
    )

    def _mlat_wikilink_repl(m):
        url = m.group(0).lstrip("[").rstrip("]")
        # Find the display text — everything after the last space before ]]
        # Split on the last space to separate URL from display text
        name = m.group(1).strip()
        return _mlat_to_coordosm(url, name)
    content = mlat_wikilink_pat.sub(_mlat_wikilink_repl, content)

    # Bare URL form
    mlat_bare_pat = re.compile(
        r"https?://(?:www\.)?openstreetmap\.org/\?"
        r"[^\s\]<>}]+"
    )

    def _mlat_bare_repl(m):
        url = m.group(0)
        return _mlat_to_coordosm(url)
    content = mlat_bare_pat.sub(_mlat_bare_repl, content)

    # ---- Pass 1d: OGF /query?lat=...&lon=... and /search?query=... URLs → {{coord}} ----
    # These open the query tool at specific coordinates.  Zoom is extracted
    # from optional #map= fragment; defaults to 13.

    def _ogf_query_to_coord(url, name=""):
        """Convert an OGF /query or /search URL to {{coord}} template."""
        lat, lon, zoom = "", "", "13"
        if "search" in url:
            # /search?whereami=1&query=LAT%2CLON#map=Z/...
            qm = re.search(r"query=([-+]?\d+(?:\.\d+)?)%2C([-+]?\d+(?:\.\d+)?)", url)
            if qm:
                lat, lon = qm.group(1), qm.group(2)
        else:
            # /query?lat=...&lon=...
            lat_m = re.search(r"lat=([-+]?\d+(?:\.\d+)?)", url)
            lon_m = re.search(r"lon=([-+]?\d+(?:\.\d+)?)", url)
            if lat_m:
                lat = lat_m.group(1)
            if lon_m:
                lon = lon_m.group(1)
        zoom_m = re.search(r"#map=(\d+)", url)
        if zoom_m:
            zoom = zoom_m.group(1)
        params = f"latitude={lat}|longitude={lon}|zoom={zoom}"
        if name:
            params += f"|name={name}"
        changes.append(f"OGF query link → {{{{coord|{params}}}}}")
        return f"{{{{coord|{params}}}}}"

    query_bare_pat = re.compile(
        r"https?://(?:www\.)?opengeofiction\.net/(?:query|search)\?"
        r"[^\s\]<>}]+"
    )

    def _query_bare_repl(m):
        return _ogf_query_to_coord(m.group(0))
    content = query_bare_pat.sub(_query_bare_repl, content)

    # ---- Pass 1e: OGF map_scale.html URLs → {{scalehelper}} ------------
    # URLs like /util/map_scale.html?map=5/ZOOM/LAT1/LON1&map2=OSM/x/LAT2/LON2
    # convert to {{scalehelper|zoom=...|lat1=...|lon1=...|lat2=...|lon2=...}}
    # with optional map1/map2 if they differ from defaults (5 and OSM).

    def _scalehelper_repl(url, name=""):
        """Convert a map_scale.html URL to {{scalehelper}} template.

        Lat/lon rounded to 5 decimal places, zoom to integer or 1 decimal.
        Returns the original URL unchanged if essential params are missing.
        """

        def _fmt(coord, decimals=5):
            """Round coordinate, strip trailing zeros after decimal."""
            s = f"{float(coord):.{decimals}f}"
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s

        def _fmt_zoom(z):
            """Zoom as integer if whole, else 1 decimal place."""
            s = f"{float(z):.1f}"
            return s.rstrip("0").rstrip(".") if s.endswith(".0") else s

        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        map1, zoom, lat1, lon1 = "5", "", "", ""
        if "map" in qs:
            parts = qs["map"][0].split("/")
            if len(parts) >= 4:
                map1_raw = parts[0]
                # C (old OGF Carto) is no longer supported by map_scale — map to 5 (standard Carto)
                if map1_raw == "C":
                    map1_raw = "5"
                map1, zoom, lat1, lon1 = map1_raw, _fmt_zoom(parts[1]), _fmt(parts[2]), _fmt(parts[3])

        # If zoom or coords are missing, the URL is malformed — leave it alone
        if not zoom or not lat1 or not lon1:
            return url

        map2, lat2, lon2 = "OSM", "", ""
        if "map2" in qs:
            parts = qs["map2"][0].split("/")
            if len(parts) >= 3:
                lat2, lon2 = _fmt(parts[-2]), _fmt(parts[-1])
                map2 = parts[0]
        params = f"zoom={zoom}|lat1={lat1}|lon1={lon1}|lat2={lat2}|lon2={lon2}"
        if map1 != "5":
            params = f"map1={map1}|" + params
        if map2 != "OSM":
            params = f"map2={map2}|" + params
        if name:
            params += f"|label={name}"
        changes.append(f"map scale link → {{{{scalehelper|{params}}}}}")
        return f"{{{{scalehelper|{params}}}}}"

    scale_bare_pat = re.compile(
        r"https?://(?:www\.)?wiki\.opengeofiction\.net/util/map_scale\.html\?"
        r"[^\s\]<>}]+"
    )

    # Wikilink form: [URL TEXT] — extract display text as label
    scale_wikilink_pat = re.compile(
        r"\[https?://(?:www\.)?wiki\.opengeofiction\.net/util/map_scale\.html\?"
        r"[^\s\]]+\s+"
        r"([^\]]+)\]"
    )

    def _scale_wikilink_repl(m):
        url_and_text = m.group(0)[1:-1]  # strip [ and ]
        name = m.group(1).strip()
        url = url_and_text[:-(len(name) + 1)]  # strip display text + space
        return _scalehelper_repl(url, name)
    content = scale_wikilink_pat.sub(_scale_wikilink_repl, content)

    def _scale_bare_repl(m):
        return _scalehelper_repl(m.group(0))
    content = scale_bare_pat.sub(_scale_bare_repl, content)

    # Clean up any {{scalehelper}} templates that were saved with map1=C
    # (before the C→5 mapping fix was added)
    def _cleanup_scale_c(m):
        changes.append("fixed stale scalehelper map1=C → default")
        return "{{scalehelper|" + m.group(1) + "}}"

    content = re.sub(
        r"\{\{scalehelper\|map1=C\|([^}]+)\}\}",
        _cleanup_scale_c,
        content
    )

    # ---- Pass 2: Replace bare territory IDs (first occurrence only) ----
    # Build alternation of all known territory IDs, longest first to avoid
    # partial matches (e.g. AN106 matching inside AN106a).
    sorted_ids = sorted(territory_map.keys(), key=len, reverse=True)
    # Escape any regex-special chars in IDs (none expected, but safe).
    escaped = [re.escape(tid) for tid in sorted_ids]
    id_pat = "|".join(escaped)

    # Match territory IDs while skipping content inside {{...}} templates
    # (so already-templated references like {{relation|314512|AN106}} are left alone)
    # and inside [[File:...]] / [[Image:...]] links (false positives like
    # [[File:AN146-Physical-2.png]] would break the file reference).
    #
    # Only the FIRST occurrence of each territory ID is ever replaced.
    # IDs that already have a {{relation|...|ID}} on the page (from a prior
    # run) are pre-seeded into seen_ids so remaining bare occurrences are
    # permanently skipped.
    seen_ids = set()
    for m in re.finditer(r"\{\{relation\|(\d+)\|([^}]+)\}\}", content):
        seen_ids.add(m.group(2))

    territory_re = re.compile(
        r"{{[^}]*}}"           # skip template spans
        r"|"
        r"\[\[(?:File|Image):[^\]]*\]\]"  # skip File:/Image: links
        r"|"
        rf"\b({id_pat})\b"  # capture territory IDs
    )

    def _tid_repl(m):
        if m.group(0).startswith("{{") or m.group(0).startswith("[[File:") or m.group(0).startswith("[[Image:"):
            return m.group(0)  # leave templates and File:/Image: links untouched
        tid = m.group(1)
        if tid in seen_ids:
            return m.group(0)  # already handled — skip all remaining occurrences
        seen_ids.add(tid)
        rid = territory_map[tid]
        changes.append(f"territory ID {tid} → {{{{relation|{rid}|{tid}}}}}")
        return f"{{{{relation|{rid}|{tid}}}}}"

    content = territory_re.sub(_tid_repl, content)

    # ---- Pass 3: Restore protected blocks ------------
    for ph, original in protected.items():
        content = content.replace(ph, original)
    for ph, original in diary_protected.items():
        content = content.replace(ph, original)

    # ---- Pass 4: Find orphan URLs (bare OGF/OSM links not converted) ----
    # These are URLs the script did not know how to handle.  Reporting them
    # helps identify new patterns that could be templatized in future.
    orphans = find_orphan_urls(content)

    return content, changes, orphans


def find_orphan_urls(content):
    """Find bare OGF/OSM URLs not inside {{...}} template invocations.

    After all replacements, any remaining opengeofiction.net or
    openstreetmap.org URL that is NOT inside a {{...}} template is
    considered an orphan — a link the script could not convert.

    OSM bare object URLs (openstreetmap.org/(way|relation|node|changeset)/ID
    without #map=) are intentionally left unconverted (no templates exist for
    real OSM objects) and are silently filtered out — they are not true
    orphans, just genuine cross-wiki references.
    """
    orphan_re = re.compile(
        r"\{\{[^}]*\}\}"                     # skip template spans
        r"|"
        r"(https?://(?:www\.)?(?:opengeofiction\.net|openstreetmap\.org)/"
        r"[^\s\]<>}]+)"
    )
    # OSM object URLs (no #map=) have no template to convert to — ignore them
    osm_object_re = re.compile(
        r"https?://(?:www\.)?openstreetmap\.org/"
        r"(?:way|relation|node|changeset)/\d+"
    )
    # Diary URLs are intentionally left unconverted — no template exists
    diary_orphan_re = re.compile(
        r"https?://(?:www\.)?opengeofiction\.net/user/[^/\s]+/diary/\d+"
    )
    seen = set()
    orphans = []
    for m in orphan_re.finditer(content):
        url = m.group(1)
        if not url or url in seen:
            continue
        # Skip OSM bare object URLs — they are genuine cross-wiki references
        if osm_object_re.match(url):
            continue
        # Skip diary URLs — intentionally left unconverted, no template exists
        if diary_orphan_re.match(url):
            continue
        seen.add(url)
        orphans.append(url)
    return orphans


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
    use_recent = "--recent" in sys.argv

    # --pages FILE: read page titles from FILE (one per line) and process all
    pages_file = None
    for i, arg in enumerate(sys.argv):
        if arg == "--pages" and i + 1 < len(sys.argv):
            pages_file = sys.argv[i + 1]
            break
    page_list = []
    if pages_file:
        try:
            with open(pages_file) as f:
                page_list = [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            print(f"Error: Pages file not found: {pages_file}")
            sys.exit(1)
        if not page_list:
            print(f"Error: No pages found in {pages_file}")
            sys.exit(1)
        print(f"Loaded {len(page_list)} page(s) from {pages_file}")

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

    if not check_bot_permission() and not dry_run:
        print("Bot permission denied, exiting")
        sys.exit(1)

    # Build the list of titles to process
    if page_list:
        titles = page_list
    elif use_recent:
        titles = get_recently_changed_pages(opener)
        if not titles:
            print("No recently changed pages found — exiting")
            sys.exit(0)
        print(f"Found {len(titles)} pages changed in the last 24 hours")
        if dry_run:
            print("Dry-run mode — listing pages that would be checked:")
            for t in titles:
                print(f"  {t}")
            sys.exit(0)
    else:
        # Decide page source based on current minute:
        #   minute < 30 → pick from Category:Territory application
        #   minute >= 30 → pick a random page (excl. Template/Admin/OpenGeofiction/Help)
        minute = datetime.datetime.now(datetime.timezone.utc).minute
        if minute < 30:
            pages = get_category_pages(opener)
            if not pages:
                print("Error: No pages found in Category:Territory application")
                sys.exit(1)
            title = random.choice(pages)
            print(f"Found {len(pages)} pages in category")
        else:
            title = get_random_page(opener)
            if not title:
                print("Error: Could not get random page")
                sys.exit(1)
            print(f"Random page selected")
        titles = [title]

    total_edits = 0
    total_pages_changed = 0
    for idx, title in enumerate(titles):
        if len(titles) > 1:
            print(f"\n--- [{idx + 1}/{len(titles)}] {title} ---")
        else:
            print(f"Selected: {title}")

        content, pageid = get_page_content(opener, title)
        if content is None:
            print(f"Error: Could not fetch content for {title}")
            if len(titles) > 1:
                continue
            sys.exit(1)
        print(f"Page ID {pageid}, {len(content)} characters")

        new_content, changes, orphans = transform_wikitext(content, territory_map)

        # Classify orphan URLs by type for the log
        if page_list:
            source = "batch"
        elif use_recent:
            source = "recent"
        else:
            source = "category" if minute < 30 else "random"
        orphan_types = {}
        for url in orphans:
            if "openstreetmap.org" in url:
                kind = "osm"
            elif "/user/" in url:
                kind = "ogf-user"
            elif "/way/" in url or "/relation/" in url or "/node/" in url or "/changeset/" in url:
                kind = "ogf-object"
            elif "/#map=" in url or "/map=" in url:
                kind = "ogf-map"
            else:
                kind = "ogf-other"
            orphan_types.setdefault(kind, []).append(url)

        # Always write to the daily book (even if no changes, orphans are valuable)
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "script": "templateFix",
            "page": title,
            "pageid": pageid,
            "source": source,
            "edits": len(changes),
            "orphan_count": len(orphans),
            "orphan_types": {k: len(v) for k, v in orphan_types.items()},
            "orphans": orphans,
            "changes": changes,
        }
        if dry_run:
            entry["dry_run"] = True
        write_daily_book(entry)

        if orphans:
            print(f"\n{len(orphans)} orphan URL(s) found (not converted):")
            for url in orphans:
                print(f"  {url}")

        if not changes:
            print("No replacements needed")
            continue

        print(f"\n{len(changes)} replacement(s) to apply:")
        for c in changes:
            print(f"  {c}")
        total_edits += len(changes)
        total_pages_changed += 1

        if dry_run:
            print("Dry-run mode — edit not saved")
            continue

        if edit_page(opener, title, new_content,
                     "Replace bare territory IDs and map URLs with wiki templates",
                     pageid):
            print(f"Successfully updated {title}")
        else:
            print(f"Failed to save edit for {title}")

        # Pause between pages in batch mode to avoid hammering the API
        if len(titles) > 1 and idx < len(titles) - 1:
            time.sleep(1)

    # Summary for batch mode
    if len(titles) > 1:
        print(f"\n=== Summary ===")
        print(f"  Pages processed: {len(titles)}")
        print(f"  Pages changed:   {total_pages_changed}")
        print(f"  Total edits:     {total_edits}")
        if dry_run:
            print("  (dry-run mode — no edits saved)")


if __name__ == "__main__":
    main()
