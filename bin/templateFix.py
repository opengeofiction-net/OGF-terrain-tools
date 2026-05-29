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

    # ---- Pass 3: Restore protected {{#multimaps:...}} blocks ------------
    for ph, original in protected.items():
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
    seen = set()
    orphans = []
    for m in orphan_re.finditer(content):
        url = m.group(1)
        if not url or url in seen:
            continue
        # Skip OSM bare object URLs — they are genuine cross-wiki references
        if osm_object_re.match(url):
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

    # Decide page source based on current minute:
    #   minute < 30 → pick from Category:Territory application
    #   minute >= 30 → pick a random page (excluding Template/Admin/OpenGeofiction/Help)
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
    print(f"Selected: {title}")

    content, pageid = get_page_content(opener, title)
    if content is None:
        print(f"Error: Could not fetch content for {title}")
        sys.exit(1)
    print(f"Page ID {pageid}, {len(content)} characters")

    new_content, changes, orphans = transform_wikitext(content, territory_map)

    # Classify orphan URLs by type for the log
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
