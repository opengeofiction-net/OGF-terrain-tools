#!/usr/bin/env python3
"""
syncDocsToWiki.py — Sync the docs-internal repo to the OGF admin wiki.

Workflow:
  1. git pull the docs-internal clone (ff-only)
  2. run `make` to rebuild any .wiki files whose .md source changed
  3. for each .md with a `wikipage:` front-matter entry, compare the built
     .wiki content against the live wiki page (trailing-whitespace-insensitive)
  4. create/edit pages whose content differs, with a git-commit-derived summary

Credentials: ~/ogf-user.env (USERNAME/PASSWORD) — same file as the other OGF bots.
Wiki auth: MediaWiki API clientlogin + csrf token, Referer header required.

Exit codes:
  0  — ran to completion (stdout may be empty = nothing to sync)
  1  — hard failure (pull/make/login) or one or more page errors
  2  — usage error

Flags:
  --dry-run   compare only, print what would change, make no edits
  --verbose   also print unchanged-page summary
"""
import datetime, glob, json, os, re, subprocess, sys, time, urllib.request, urllib.parse, http.cookiejar

API = "https://wiki.opengeofiction.net/api.php"
REFERER = "https://opengeofiction.net/"
UA = "OGF-docs-internal-sync/1.0 (bot; Brothie)"
BASE = os.path.expanduser("~/sync-ogf-docs-internal")
REPO = os.path.join(BASE, "docs-internal")
# Shared daily book lives in the OGF-terrain-tools repo's var/ (next to this
# script), the same daily-book-*.ndjson that userPatrol/templateFix/revertQueue
# write and dailyReview.py reads.
VAR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "var")
ENV_FILE = os.path.expanduser("~/ogf-user.env")
EDIT_PAUSE_S = 1.0

DRY_RUN = "--dry-run" in sys.argv
VERBOSE = "--verbose" in sys.argv


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- git + make
def sh(args, cwd=None, timeout=300):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def git(args, cwd=REPO):
    rc, out, err = sh(["git", "-C", cwd] + args)
    return rc, out, err


def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def step_pull():
    rc, out, err = git(["pull", "--ff-only", "-q"])
    if rc != 0:
        return "?", "", f"git pull failed: {err or out}"
    rc, head, _ = git(["rev-parse", "--short", "HEAD"])
    rc2, subject, _ = git(["log", "-1", "--format=%s"])
    return (head if rc == 0 else "?"), (subject if rc2 == 0 else ""), None


def step_make():
    rc, out, err = sh(["make", "-C", REPO], timeout=600)
    if rc != 0:
        return f"make failed (exit {rc}): {err or out}"
    return None


def md_last_commit_subject(md_rel):
    """Subject of the last commit touching this .md file (relative path)."""
    rc, out, _ = git(["log", "-1", "--format=%s", "--", md_rel])
    return out if rc == 0 else ""


def wikipage_and_builds():
    """Return list of (wikipage_title, md_relpath, built_wiki_path)."""
    found = []
    warns = []
    for md in sorted(glob.glob(os.path.join(REPO, "*.md"))):
        name = os.path.basename(md)
        if name in ("README.md", "AGENTS.md", "CLAUDE.md"):
            continue
        wp = None
        with open(md) as f:
            for line in f:
                s = line.strip()
                if s.startswith("wikipage:"):
                    wp = s.split(":", 1)[1].strip().strip('"').strip("'")
                    break
                if s == "---" and wp is None:
                    continue
        if not wp:
            continue
        wiki_path = os.path.splitext(md)[0] + ".wiki"
        if not os.path.exists(wiki_path):
            warns.append(f"no built .wiki for {name} (missing from Makefile DOCS?)")
            continue
        found.append((wp, name, wiki_path))
    return found, warns


# ---------------------------------------------------------------- wiki api
class Wiki:
    def __init__(self, env):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.op.addheaders = [
            ("User-Agent", UA),
            ("Accept", "application/json"),
            ("Referer", REFERER),
        ]
        self.env = env
        self.csrf = None

    def call(self, params, data=None):
        url = API + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method="POST" if data is not None else "GET")
        if data is not None:
            req.data = urllib.parse.urlencode(data).encode()
        with self.op.open(req, timeout=120) as r:
            return json.loads(r.read().decode())

    def login(self):
        tok = self.call({"action": "query", "meta": "tokens", "type": "login", "format": "json"})
        ltok = tok["query"]["tokens"]["logintoken"]
        res = self.call({"action": "clientlogin", "format": "json"}, data={
            "username": self.env.get("USERNAME", ""),
            "password": self.env.get("PASSWORD", ""),
            "loginreturnurl": "https://wiki.opengeofiction.net/index.php/Main_Page",
            "logintoken": ltok,
        })
        status = res.get("clientlogin", {}).get("status")
        if status != "PASS":
            return f"wiki login failed: {status} {res.get('clientlogin', {}).get('message', '')}"
        tok = self.call({"action": "query", "meta": "tokens", "type": "csrf", "format": "json"})
        self.csrf = tok["query"]["tokens"]["csrftoken"]
        return None

    def fetch_contents(self, titles):
        """Return {title: content_or_None(missing)}."""
        out = {}
        for i in range(0, len(titles), 20):
            chunk = titles[i:i + 20]
            data = self.call({"action": "query", "titles": "|".join(chunk),
                              "prop": "revisions", "rvprop": "content", "rvslots": "main",
                              "format": "json", "formatversion": "2"})
            for p in data["query"]["pages"]:
                if "missing" in p:
                    out[p["title"]] = None
                else:
                    try:
                        out[p["title"]] = p["revisions"][0]["slots"]["main"]["content"]
                    except (KeyError, IndexError):
                        out[p["title"]] = ""
        return out

    def edit(self, title, text, summary):
        res = self.call({"action": "edit", "format": "json"}, data={
            "title": title, "text": text, "summary": summary,
            "token": self.csrf, "bot": "1",
        })
        e = res.get("edit", {})
        if e.get("result") == "Success":
            return None, e.get("newrevid")
        # retry once on conflict
        if e.get("code") == "editconflict":
            time.sleep(1)
            res = self.call({"action": "edit", "format": "json"}, data={
                "title": title, "text": text, "summary": summary,
                "token": self.csrf, "bot": "1",
            })
            e = res.get("edit", {})
            if e.get("result") == "Success":
                return None, e.get("newrevid")
        return f"edit failed for {title}: {e.get('code', '')} {e.get('info', '')}", None


def main():
    os.makedirs(VAR, exist_ok=True)

    # 1. pull
    head, subject, err = step_pull()
    if err:
        print(err)
        return 1

    # 2. make
    err = step_make()
    if err:
        print(err)
        return 1

    # 3. mapping
    pages, warns = wikipage_and_builds()
    if not pages:
        print("ERROR: no pages with wikipage front matter found")
        return 1

    # 4. login
    if not os.path.exists(ENV_FILE):
        print(f"ERROR: {ENV_FILE} not found")
        return 1
    env = load_env(ENV_FILE)
    wiki = Wiki(env)
    err = wiki.login()
    if err:
        print(err)
        return 1

    # 5. fetch live content
    titles = [p[0] for p in pages]
    live = wiki.fetch_contents(titles)

    def norm(t):
        return t.rstrip()

    to_create, to_update, unchanged = [], [], []
    errors = []
    for title, md_rel, wiki_path in pages:
        with open(wiki_path) as f:
            local_text = f.read()
        live_text = live.get(title)
        if live_text is None:
            to_create.append((title, md_rel, local_text))
        elif norm(live_text) == norm(local_text):
            unchanged.append(title)
        else:
            to_update.append((title, md_rel, local_text))

    # 6. apply
    updated, created = [], []
    for title, md_rel, text in to_update:
        summary = "docs-internal: " + (md_last_commit_subject(md_rel) or "content update")
        if DRY_RUN:
            log(f"[dry-run] would update {title} ({summary})")
            updated.append(title)
            continue
        err, _ = wiki.edit(title, text, summary[:250])
        if err:
            errors.append(err)
            log(err)
        else:
            log(f"updated {title} ({summary})")
            updated.append(title)
        time.sleep(EDIT_PAUSE_S)

    for title, md_rel, text in to_create:
        summary = "docs-internal: import " + (md_last_commit_subject(md_rel) or title)
        if DRY_RUN:
            log(f"[dry-run] would create {title} ({summary})")
            created.append(title)
            continue
        err, _ = wiki.edit(title, text, summary[:250])
        if err:
            errors.append(err)
            log(err)
        else:
            log(f"created {title} ({summary})")
            created.append(title)
        time.sleep(EDIT_PAUSE_S)

    # 7. daily book entry — shared with the other OGF bots, every real run
    #    (even no-change runs), so the daily review always sees a docsSync row.
    #    Dry-runs are manual and never written to the book.
    if not DRY_RUN:
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "script": "docsSync",
            "commit": head or "",
            "dry_run": DRY_RUN,
            "updated": updated,
            "created": created,
            "unchanged": len(unchanged),
            "errors": errors,
            "warnings": warns,
        }
        book = os.path.join(VAR, f"daily-book-{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}.ndjson")
        with open(book, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 8. stdout report (delivered) — only when there is something to say
    if not DRY_RUN and not errors and not updated and not created and not warns:
        return 0  # fully silent
    headline = head or "?"
    if DRY_RUN:
        print(f"docs-internal sync DRY-RUN at {headline}: {len(updated)} to update, {len(created)} to create, {len(unchanged)} unchanged")
    elif errors:
        print(f"docs-internal sync at {headline}: {len(updated)} updated, {len(created)} created, {len(errors)} ERROR(S)")
    else:
        print(f"docs-internal sync at {headline}: {len(updated)} page(s) updated, {len(created)} created, {len(unchanged)} unchanged")
    for t in created:
        print(f"  CREATED {t}")
    for t in updated:
        print(f"  UPDATED {t}")
    for w in warns:
        print(f"  WARN {w}")
    for e in errors:
        print(f"  ERROR {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
