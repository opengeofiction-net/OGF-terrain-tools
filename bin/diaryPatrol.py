#!/usr/bin/env python3
"""
OpenGeofiction Diary Patrol - Hide non-admin/moderator diary entries after May 1, 2026

Only admin accounts or users with admin/moderator permissions should post diary entries.
This script checks the first page of the diary, identifies non-admin posts after
May 1, 2026, and hides them.

Uses web session authentication (same as Brothie user).
"""

import requests
import re
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

# ─── Configuration ───────────────────────────────────────────────────────────

OGF_URL = "https://opengeofiction.net"
DIARY_URL = f"{OGF_URL}/diary"
LOGIN_URL = f"{OGF_URL}/login"
API_URL = f"{OGF_URL}/api/0.6/user/"

# Entries after this date from non-admin users should be hidden
CUTOFF_DATE = datetime(2026, 5, 1, tzinfo=timezone.utc)

# Credentials
CREDENTIALS_PATH = os.path.expanduser("~/ogf-user.env")
SESSION_PATH = os.path.expanduser("~/.hermes/opengeofiction/patrol/ogf_session.json")

USER_AGENT = "OGF-DiaryPatrol/1.0 (Brothie adminbot)"
REFERER = "https://opengeofiction.net/"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def load_credentials():
    """Load Brothie credentials from ~/.ogf-user.env"""
    if not os.path.exists(CREDENTIALS_PATH):
        print(f"ERROR: Credentials file not found: {CREDENTIALS_PATH}")
        sys.exit(1)
    
    creds = {}
    with open(CREDENTIALS_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                creds[key.strip()] = val.strip()
    
    if "USERNAME" not in creds or "PASSWORD" not in creds:
        print("ERROR: USERNAME and PASSWORD required in credentials file")
        sys.exit(1)
    
    return creds["USERNAME"], creds["PASSWORD"]


def save_session(session, path=SESSION_PATH):
    """Save session cookies to disk for reuse"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cookies = {}
    for c in session.cookies:
        cookies[c.name] = {
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "expires": c.expires,
            "secure": c.secure,
            "rest": c._rest
        }
    with open(path, "w") as f:
        json.dump(cookies, f)


def load_session(session, path=SESSION_PATH):
    """Load session cookies from disk if available"""
    if not os.path.exists(path):
        return False
    
    try:
        with open(path, "r") as f:
            cookies = json.load(f)
        
        for name, attrs in cookies.items():
            # Skip expired cookies
            if attrs.get("expires") and attrs["expires"] < time.time():
                continue
            
            import http.cookiejar as cookielib
            c = cookielib.Cookie(
                version=0, name=name, value=attrs["value"],
                port=None, port_specified=False,
                domain=attrs.get("domain", ".opengeofiction.net"),
                domain_specified=True, domain_initial_dot=False,
                path=attrs.get("path", "/"), path_specified=True,
                secure=attrs.get("secure", False),
                expires=attrs.get("expires"),
                discard=False,
                comment=None, comment_url=None,
                rest=attrs.get("rest", {})
            )
            session.cookies.set_cookie(c)
        
        return True
    except Exception as e:
        print(f"WARNING: Failed to load session: {e}")
        return False


def login(session, username, password):
    """Log in to OGF using web session auth"""
    # Get login page (gets CSRF token and sets cookies)
    resp = session.get(LOGIN_URL, headers={
        "User-Agent": USER_AGENT,
        "Referer": REFERER
    })
    resp.raise_for_status()
    
    # Extract CSRF token from meta tag
    csrf_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', resp.text)
    if not csrf_match:
        print("ERROR: Could not find CSRF token on login page")
        sys.exit(1)
    csrf_token = csrf_match.group(1)
    
    # Submit login form (field names are "username" and "password", not "user[login]" etc.)
    resp = session.post(LOGIN_URL, data={
        "username": username,
        "password": password,
        "remember_me": "1",
        "authenticity_token": csrf_token
    }, headers={
        "User-Agent": USER_AGENT,
        "Referer": LOGIN_URL,
        "X-CSRF-Token": csrf_token
    })
    resp.raise_for_status()
    
    # Check if login succeeded (check for flash error div)
    if "flash error" in resp.text or "Sorry, could not log in" in resp.text:
        print("ERROR: Login failed - check credentials")
        sys.exit(1)
    
    print(f"✓ Logged in as {username}")
    save_session(session)
    return True


def is_admin_or_moderator(session, user_id):
    """Check if a user has admin or moderator role via the OGF API"""
    try:
        resp = session.get(f"{API_URL}{user_id}", headers={
            "User-Agent": USER_AGENT,
            "Referer": REFERER
        })
        resp.raise_for_status()
        
        # Parse XML response for roles
        if b"<administrator/>" in resp.content or b"<moderator/>" in resp.content:
            return True
        
        return False
    except Exception as e:
        print(f"WARNING: Failed to check roles for user {user_id}: {e}")
        return False  # Conservative: treat unknown users as non-admin


def parse_diary_page(html):
    """Parse the diary page and extract entry data for visible entries only"""
    entries = []
    
    # Find all diary_post divs that are visible (not deleted)
    # Visible: class contains "diary_post" but NOT "deleted"
    # Hidden: class contains "diary_post text-muted px-3 deleted"
    
    # Pattern to find diary post containers (OGF uses single quotes in HTML)
    pattern = r"<div class='([^']*diary_post[^']*)'>"
    for match in re.finditer(pattern, html):
        classes = match.group(1)
        
        # Skip hidden entries
        if "deleted" in classes:
            continue
        
        # Extract user ID from CSS class (e.g., "diary_post user_29515" -> 29515)
        user_id_match = re.search(r'user_(\d+)', classes)
        if not user_id_match:
            continue
        user_id = user_id_match.group(1)
        
        # Get the full div content (up to the next diary_post or end)
        start = match.start()
         # Find the next diary_post div or end of relevant section
        next_match = re.search(pattern, html[start + 1:])
        if next_match:
            end = start + 1 + next_match.start()
        else:
            end = len(html)
        
        div_content = html[start:end]
        
        # Extract entry title (h2 link text)
        title_match = re.search(r'<h2[^>]*>\s*<a[^>]*>([^<]+)</a>', div_content)
        if not title_match:
            continue
        title = title_match.group(1).strip()
        
        # Extract author username (link to /user/username)
        author_match = re.search(r'<a\s+href="/user/([^/]+?)/diary/\d+', div_content)
        if not author_match:
            continue
        username = author_match.group(1)
        
        # Extract entry ID
        entry_id_match = re.search(r'/diary/(\d+)', author_match.group(0))
        if not entry_id_match:
            continue
        entry_id = entry_id_match.group(1)
        
        # Extract date (format: "on DD Month YYYY")
        date_match = re.search(r'on\s+(\d{1,2})\s+(\w+)\s+(\d{4})', div_content)
        if not date_match:
            continue
        
        day = int(date_match.group(1))
        month_str = date_match.group(2)
        year = int(date_match.group(3))
        
        # Convert month name to number
        months = {
            "January": 1, "February": 2, "March": 3, "April": 4,
            "May": 5, "June": 6, "July": 7, "August": 8,
            "September": 9, "October": 10, "November": 11, "December": 12
        }
        month = months.get(month_str, 0)
        
        if month == 0:
            print(f"WARNING: Unknown month '{month_str}' for entry '{title}'")
            continue
        
        entry_date = datetime(year, month, day, tzinfo=timezone.utc)
        
        entries.append({
            "title": title,
            "username": username,
            "user_id": user_id,
            "entry_id": entry_id,
            "date": entry_date,
            "date_str": f"{day} {month_str} {year}"
        })
    
    return entries


def hide_entry(session, username, entry_id):
    """Hide a diary entry via POST to /user/username/diary/entry_id/hide"""
    # Need CSRF token from the diary page
    resp = session.get(DIARY_URL, headers={
        "User-Agent": USER_AGENT,
        "Referer": REFERER
    })
    resp.raise_for_status()
    
    # Extract CSRF token
    csrf_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', resp.text)
    if not csrf_match:
        print(f"  ERROR: Could not find CSRF token")
        return False
    csrf_token = csrf_match.group(1)
    
    # POST to hide
    hide_url = f"{OGF_URL}/user/{username}/diary/{entry_id}/hide"
    resp = session.post(hide_url, headers={
        "User-Agent": USER_AGENT,
        "Referer": DIARY_URL,
        "X-CSRF-Token": csrf_token,
        "X-Requested-With": "XMLHttpRequest"
    })
    
    # Check if hide succeeded
    # Rails UJS redirects after successful action
    if resp.status_code in (200, 302, 304, 204):
        return True
    else:
        print(f"  ERROR: Hide failed with status {resp.status_code}")
        return False


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    username, password = load_credentials()
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": REFERER
    })
    
    # Try to load existing session
    loaded = load_session(session)
    
    # Login if no session or session not valid
    if not loaded:
        print("No existing session found, logging in...")
        login(session, username, password)
    else:
        # Verify session still works
        resp = session.get(OGF_URL + "/", headers={"Referer": REFERER})
        if "Log In" in resp.text and "Brothie" not in resp.text:
            print("Session expired, logging in...")
            login(session, username, password)
        else:
            print(f"✓ Reusing existing session (logged in as Brothie)")
    
    # Fetch diary page
    print(f"\nFetching diary page: {DIARY_URL}")
    resp = session.get(DIARY_URL, headers={
        "User-Agent": USER_AGENT,
        "Referer": REFERER
    })
    resp.raise_for_status()
    
    # Parse entries
    entries = parse_diary_page(resp.text)
    print(f"Found {len(entries)} visible diary entries on first page\n")
    
    # Process each entry
    to_hide = []
    for entry in entries:
        # Check if entry is after cutoff date
        if entry["date"] < CUTOFF_DATE:
            continue
        
        # Check if user is admin/moderator
        is_admin = is_admin_or_moderator(session, entry["user_id"])
        
        if not is_admin:
            to_hide.append(entry)
            print(f"  [{entry['date_str']}] @{entry['username']} - '{entry['title']}' -> HIDE")
        else:
            print(f"  [{entry['date_str']}] @{entry['username']} - '{entry['title']}' -> OK (admin/mod)")
    
    if not to_hide:
        print("\n✓ No entries need hiding")
        return 0
    
    print(f"\nHiding {len(to_hide)} entries...")
    hidden_count = 0
    for entry in to_hide:
        print(f"  Hiding: {entry['title']} by {entry['username']} (entry #{entry['entry_id']})")
        if hide_entry(session, entry["username"], entry["entry_id"]):
            hidden_count += 1
            print(f"    ✓ Hidden")
        else:
            print(f"    ✗ Failed")
    
    print(f"\nDone. Hidden {hidden_count}/{len(to_hide)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
