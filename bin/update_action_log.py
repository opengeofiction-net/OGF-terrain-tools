#!/usr/bin/env python3
"""Update the User:Brothie/action log wiki page by prepending new content."""
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

WIKI_API_URL = "https://wiki.opengeofiction.net/api.php"
USER_AGENT = "Brothie/1.0 (OGF Bot)"
REFERER = "https://opengeofiction.net/"
CREDENTIALS_PATH = Path.home() / "ogf-user.env"
REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 3
RETRY_DELAY = 10


def load_credentials():
    creds = {}
    with open(CREDENTIALS_PATH) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                creds[key.strip()] = value.strip().strip('"').strip("'")
    return creds


def _build_request(url, data=None):
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Referer", REFERER)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return req


def api_get(opener, params):
    url = f"{WIKI_API_URL}?{urllib.parse.urlencode(params)}"
    req = _build_request(url)
    try:
        resp = opener.open(req, timeout=REQUEST_TIMEOUT)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"HTTP Error {exc.code}: {exc.reason}", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return None


def api_post(opener, params):
    data = urllib.parse.urlencode(params).encode()
    req = _build_request(WIKI_API_URL, data)
    try:
        resp = opener.open(req, timeout=REQUEST_TIMEOUT)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"HTTP Error {exc.code}: {exc.reason}", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return None


def login(opener, username, password):
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


def get_page_content(opener, title):
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
        return None, None
    for pid, page in data.get("query", {}).get("pages", {}).items():
        if pid == "-1":
            # Page does not exist yet (as opposed to a fetch error)
            return None, -1
        revs = page.get("revisions", [])
        if revs:
            return revs[0].get("*", ""), page.get("pageid")
    return None, None


def edit_page(opener, title, text, summary):
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
        if "error" in result:
            err = result["error"]
            print(f"Edit API error: {err.get('code')} — {err.get('info', '')[:200]}", file=sys.stderr)
            return False
        return result.get("edit", {}).get("result") == "Success"
    return False


def main():
    content_path = sys.argv[1] if len(sys.argv) > 1 else None
    if content_path:
        with open(content_path) as fh:
            new_content = fh.read()
    else:
        new_content = sys.stdin.read()

    creds = load_credentials()
    username = creds.get("USERNAME", "")
    password = creds.get("PASSWORD", "")
    if not username or not password:
        print("Error: No credentials", file=sys.stderr)
        sys.exit(1)

    title = "User:Brothie/action log"

    # The wiki's session handling can be transiently flaky (login token
    # issued under a session that is gone by POST time), so retry the whole
    # login -> fetch -> prepend -> save sequence with a fresh session each time.
    for attempt in range(1, MAX_ATTEMPTS + 1):
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj)
        )

        if not login(opener, username, password):
            print(f"Error: Login failed (attempt {attempt}/{MAX_ATTEMPTS})", file=sys.stderr)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY)
            continue

        # Fetch current content. Distinguish "page missing" (-1) from a
        # transient fetch failure (None) so a failed fetch triggers a retry
        # instead of clobbering the page with a fresh one.
        current, pageid = get_page_content(opener, title)
        if pageid == -1:
            print("Creating new page (no existing content)")
            final = new_content
        elif current is None:
            print(f"Error: Failed to fetch current content (attempt {attempt}/{MAX_ATTEMPTS})", file=sys.stderr)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY)
            continue
        else:
            final = new_content + "\n" + current

        if edit_page(opener, title, final, "Bot: daily action log update"):
            print("Wiki page updated successfully")
            return

        print(f"Error: Failed to save (attempt {attempt}/{MAX_ATTEMPTS})", file=sys.stderr)
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY)

    print("Error: All attempts failed", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
