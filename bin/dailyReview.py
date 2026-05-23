#!/usr/bin/env python3
"""
dailyReview.py - Aggregate daily bot activity from daily-book NDJSON files.

Reads the day's JSONL entries, produces a human-readable summary (for Discord),
and updates the wiki page User:Brothie/action log.

Usage:
    python3 bin/dailyReview.py              # Review today
    python3 bin/dailyReview.py 2026-05-15    # Review a specific date
    python3 bin/dailyReview.py --dry-run     # Preview without wiki update
"""

import datetime
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
VAR_DIR = Path(__file__).parent.parent / "var"
WIKI_API_URL = "https://wiki.opengeofiction.net/api.php"
WIKI_PAGE = "User:Brothie/action log"
USER_AGENT = "Brothie/1.0 (OGF Daily Review)"
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
    """Create a urllib Request with required headers."""
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Referer", REFERER)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return req


def api_get(opener, params):
    """GET from MediaWiki API. Returns parsed JSON or None."""
    url = f"{WIKI_API_URL}?{urllib.parse.urlencode(params)}"
    req = _build_request(url)
    try:
        resp = opener.open(req)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"  HTTP Error {exc.code}: {exc.reason}")
        return None


def api_post(opener, params):
    """POST to MediaWiki API. Returns parsed JSON or None."""
    data = urllib.parse.urlencode(params).encode()
    req = _build_request(WIKI_API_URL, data)
    try:
        resp = opener.open(req)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"  HTTP Error {exc.code}: {exc.reason}")
        return None


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


def get_page_content(opener, title):
    """Fetch wikitext content. Returns (content, pageid) or (None, None)."""
    data = api_get(opener, {
        "action": "query", "titles": title,
        "prop": "revisions", "rvprop": "content", "format": "json",
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


def edit_page(opener, title, text, summary, pageid=None):
    """Save wikitext via the API. Returns True on success."""
    token_data = api_get(opener, {
        "action": "query", "meta": "tokens",
        "type": "csrf", "format": "json",
    })
    if not token_data:
        return False
    csrf = token_data["query"]["tokens"]["csrftoken"]

    params = {
        "action": "edit", "title": title,
        "text": text, "summary": summary,
        "token": csrf, "bot": "1", "format": "json",
    }
    if pageid:
        params["pageid"] = str(pageid)

    result = api_post(opener, params)
    if result:
        return result.get("edit", {}).get("result") == "Success"
    return False


# ---------------------------------------------------------------------------
# Load and aggregate
# ---------------------------------------------------------------------------
def load_daily_book(date_str):
    """Load JSONL entries for a given date."""
    path = VAR_DIR / f"daily-book-{date_str}.ndjson"
    if not path.exists():
        return []
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def aggregate(entries):
    """Group entries by script and compute summaries."""
    by_script = defaultdict(list)
    for entry in entries:
        by_script[entry.get("script", "unknown")].append(entry)
    return dict(by_script)


def track_user_violation_growth(today_date_str, up_entries):
    """Compare today's patrol users against previous days for violation growth.

    Returns a dict with 'growing', 'new_users', and 'stable_count',
    or None if no user-level data is available (old-format entries).
    """
    # Step 1: Build today's user profiles (max violation per user across all runs)
    today_users = {}
    for e in up_entries:
        for u in e.get("users", []):
            name = u["name"]
            if name not in today_users or u["violations"] > today_users[name]["violations"]:
                today_users[name] = {
                    "violations": u["violations"],
                    "classification": u.get("classification", ""),
                    "notified": u.get("notified", False),
                }

    if not today_users:
        return None  # Old-format entries without user data

    # Step 2: Load previous days' patrol entries and build historical profiles
    today = datetime.datetime.strptime(today_date_str, "%Y-%m-%d").date()
    historical = {}  # name -> {peak_violations, peak_day, days_flagged}

    for days_back in range(1, 8):
        prev_date = today - datetime.timedelta(days=days_back)
        prev_str = prev_date.strftime("%Y-%m-%d")
        prev_entries = load_daily_book(prev_str)
        for e in prev_entries:
            if e.get("script") != "userPatrol":
                continue
            for u in e.get("users", []):
                name = u["name"]
                if name not in historical:
                    historical[name] = {
                        "peak_violations": 0,
                        "peak_day": "",
                        "days_flagged": set(),
                    }
                historical[name]["days_flagged"].add(prev_str)
                if u["violations"] > historical[name]["peak_violations"]:
                    historical[name]["peak_violations"] = u["violations"]
                    historical[name]["peak_day"] = prev_str

    # Step 3: Classify today's users
    growing = []
    new_users = []
    stable_count = 0

    for name, info in sorted(today_users.items(), key=lambda x: -x[1]["violations"]):
        today_v = info["violations"]
        if name in historical:
            hist = historical[name]
            peak = hist["peak_violations"]
            days_flagged = len(hist["days_flagged"])

            if today_v > peak:
                growth = today_v - peak
                growing.append({
                    "name": name,
                    "previous": peak,
                    "prev_day": hist["peak_day"],
                    "today": today_v,
                    "growth": growth,
                    "classification": info["classification"],
                    "days_active": days_flagged + 1,
                })
            else:
                stable_count += 1
        else:
            new_users.append({"name": name, **info})

    return {
        "growing": growing,
        "new_users": new_users,
        "stable_count": stable_count,
    }


def format_summary(date_str, by_script):
    """Format a human-readable summary for Discord."""
    lines = [f"== {date_str} =="]

    # templateFix
    tf_entries = by_script.get("templateFix", [])
    if tf_entries:
        total_runs = len(tf_entries)
        edits_made = sum(1 for e in tf_entries if e.get("edits", 0) > 0)
        total_edits = sum(e.get("edits", 0) for e in tf_entries)
        pages_checked = len(set(e.get("page", "") for e in tf_entries))
        cat_runs = sum(1 for e in tf_entries if e.get("source") == "category")
        random_runs = total_runs - cat_runs

        # Change type breakdown
        change_types = Counter()
        for e in tf_entries:
            for c in e.get("changes", []):
                if "territory ID" in c:
                    change_types["territory ID"] += 1
                elif "map link" in c:
                    change_types["coord"] += 1
                elif "osm" in c:
                    change_types["coordosm"] += 1
                elif "user" in c:
                    change_types["OGF user"] += 1
                elif any(t in c for t in ["way", "relation", "node", "changeset"]):
                    change_types["object template"] += 1
                else:
                    change_types["other"] += 1

        # Orphan URL breakdown
        orphan_types = Counter()
        total_orphan_urls = 0
        orphan_pages = set()
        for e in tf_entries:
            orphans = e.get("orphans", [])
            total_orphan_urls += len(orphans)
            if orphans:
                orphan_pages.add(e.get("page", ""))
            ot = e.get("orphan_types", {})
            for k, v in ot.items():
                orphan_types[k] += v

        lines.append("")
        lines.append("=== templateFix ===")
        lines.append(f"* {total_runs} runs ({cat_runs} category, {random_runs} random)")
        lines.append(f"* {edits_made} pages edited with {total_edits} total replacements")
        if change_types:
            for ct, count in change_types.most_common():
                lines.append(f"** {ct}: {count}")
        if total_orphan_urls:
            lines.append(f"* {total_orphan_urls} orphan URL(s) on {len(orphan_pages)} page(s)")
            for ot, count in orphan_types.most_common():
                lines.append(f"** {ot}: {count}")
    else:
        lines.append("")
        lines.append("=== templateFix ===")
        lines.append("* No runs recorded")

    # userPatrol
    up_entries = by_script.get("userPatrol", [])
    if up_entries:
        total_users = sum(e.get("users_checked", 0) for e in up_entries)
        total_notified = sum(e.get("notified", 0) for e in up_entries)
        class_totals = Counter()
        for e in up_entries:
            for cls, count in e.get("classifications", {}).items():
                class_totals[cls] += count
        lines.append("")
        lines.append("=== userPatrol ===")
        lines.append(f"* {len(up_entries)} runs, {total_users} users checked")
        lines.append(f"* {total_notified} notifications sent")
        if class_totals:
            for cls, count in class_totals.most_common():
                lines.append(f"** {cls}: {count}")

        # Violation growth tracking
        growth_data = track_user_violation_growth(date_str, up_entries)
        if growth_data:
            if growth_data["growing"]:
                lines.append("")
                lines.append("**Users with growing violations:**")
                lines.append("| User | Previous | Today | Growth | Classification | Days active |")
                lines.append("|------|----------|-------|--------|----------------|-------------|")
                for u in growth_data["growing"]:
                    prev_str = f"{u['previous']} ({u['prev_day']})" if u['prev_day'] else str(u['previous'])
                    lines.append(
                        f"| {u['name']} | {prev_str} | {u['today']} "
                        f"| +{u['growth']} | {u['classification']} | {u['days_active']}d |"
                    )
            if growth_data["new_users"]:
                new_details = ", ".join(
                    f"{u['name']} ({u['classification']}, {u['violations']} violations)"
                    for u in growth_data["new_users"]
                )
                lines.append("")
                lines.append(f"**New users flagged:** {new_details}")
            if growth_data["stable_count"] > 0:
                lines.append("")
                lines.append(f"**Stable users:** {growth_data['stable_count']} — violation counts unchanged")
    else:
        lines.append("")
        lines.append("=== userPatrol ===")
        lines.append("* No runs recorded")

    # revertQueue
    rq_entries = by_script.get("revertQueue", [])
    if rq_entries:
        total_reverts = sum(e.get("reverts", 0) for e in rq_entries)
        total_failures = sum(e.get("failures", 0) for e in rq_entries)
        total_pending = sum(e.get("pending", 0) for e in rq_entries)
        lines.append("")
        lines.append("=== revertQueue ===")
        lines.append(f"* {len(rq_entries)} runs, {total_reverts} reverts executed")
        if total_failures:
            lines.append(f"* {total_failures} failure(s)")
        if total_pending:
            lines.append(f"* {total_pending} pending request(s)")
    else:
        lines.append("")
        lines.append("=== revertQueue ===")
        lines.append("* No runs recorded")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv

    # Determine date
    date_arg = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            date_arg = arg
            break
    if date_arg:
        date_str = date_arg
    else:
        date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    entries = load_daily_book(date_str)
    if not entries:
        print(f"No daily book entries for {date_str}")
        sys.exit(0)

    by_script = aggregate(entries)
    summary = format_summary(date_str, by_script)
    print(summary)

    if dry_run:
        print("\nDry-run mode — wiki page not updated")
        sys.exit(0)

    # Update wiki page
    creds = load_credentials()
    username = creds.get("USERNAME", "")
    password = creds.get("PASSWORD", "")
    if not username or not password:
        print("Error: No credentials for wiki update")
        sys.exit(1)

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj)
    )

    if not login(opener, username, password):
        print("Error: Wiki login failed")
        sys.exit(1)
    print("Logged in to wiki")

    content, pageid = get_page_content(opener, WIKI_PAGE)
    if content is None:
        # Page doesn't exist yet — create it
        new_content = summary + "\n"
    else:
        # Prepend today's summary
        new_content = summary + "\n\n" + content

    if edit_page(opener, WIKI_PAGE, new_content,
                 f"Daily bot activity log for {date_str}"):
        print(f"\nWiki page updated: {WIKI_PAGE}")
    else:
        print(f"\nFailed to update wiki page")
        sys.exit(1)


if __name__ == "__main__":
    main()