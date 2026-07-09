#!/usr/bin/env python3
"""Update the User:Brothie/action log wiki page by prepending new content."""
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

WIKI_API_URL = "https://wiki.opengeofiction.net/api.php"
USER_AGENT = "Brothie/1.0 (OGF Bot)"
REFERER = "https://opengeofiction.net/"
CREDENTIALS_PATH = Path.home() / "ogf-user.env"


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
        resp = opener.open(req)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"HTTP Error {exc.code}: {exc.reason}", file=sys.stderr)
        return None


def api_post(opener, params):
    data = urllib.parse.urlencode(params).encode()
    req = _build_request(WIKI_API_URL, data)
    try:
        resp = opener.open(req)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"HTTP Error {exc.code}: {exc.reason}", file=sys.stderr)
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
            return None, None
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
        return result.get("edit", {}).get("result") == "Success"
    return False


def main():
    import sys
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

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj)
    )

    if not login(opener, username, password):
        print("Error: Login failed", file=sys.stderr)
        sys.exit(1)

    # Fetch current content
    title = "User:Brothie/action log"
    current, pageid = get_page_content(opener, title)
    if current is None:
        print(f"Creating new page (no existing content)")
        final = new_content
    else:
        final = new_content + "\n" + current

    if edit_page(opener, title, final, "Bot: daily action log update"):
        print("Wiki page updated successfully")
    else:
        print("Error: Failed to save", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
