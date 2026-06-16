#!/usr/bin/env python3
"""
applyLocaleSubs.py

Transforms OpenStreetMap references to OpenGeofiction in locale files.
Run this after pulling upstream translations.

Idempotent: safe to run multiple times — OGF strings will not match
the OSM search patterns, so re-running produces no further changes.

After automated substitutions, OGF-specific key overrides are applied
from etc/applyLocaleSubs.en.yml (relative to this script). Overrides
are only applied to en.yml; they replace the entire value for a key
rather than doing token substitution.

Usage:
  applyLocaleSubs.py [--dry-run] [--verbose] <repo_root>

Example:
  applyLocaleSubs.py ../openstreetmap-website/
"""

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Substitution rules — applied in order, most specific first.
# Every (old, new) pair is applied to every line of every file.
# Rules are strings, not regex; no unintentional partial matches.
#
# INTENTIONALLY NOT SUBSTITUTED (documented here for auditing):
#
#   switch2osm.org          — generic resource for OSM-based infra switching;
#                             useful context for OGF users too.
#   osmfoundation.org       — all URLs mapped to appropriate OGF wiki pages.
#   dmca.openstreetmap.org  — mapped to OpenGeofiction:Contact.
#   State of the Map        — OSM trademark; OGF has no equivalent conference
#                             name yet. Left for manual review.
#   ODbL                    — the licence itself; do not rename.
#   OSM (bare acronym)      — too many false positives in technical strings
#                             (JOSM, osm_type, osmChange XML, etc.).
#                             Replace only the compound phrases listed below.
#
# SKIP_SUBSTRINGS: if any of these appear on a line, skip substitution for
# that line entirely. Used to protect external org/provider names that happen
# to contain "OpenStreetMap".
# ---------------------------------------------------------------------------

SKIP_SUBSTRINGS = [
    # "osm_france:" key — tile layer attribution for OpenStreetMap France,
    # a specific French community organisation, not part of OGF.
    "osm_france:",
    # "hotosm_name:" key — Humanitarian OpenStreetMap Team, a specific NGO.
    "hotosm_name:",
    # switch2osm.org — intentionally kept, see note above.
    "switch2osm.org",
]

SUBSTITUTIONS = [
    # -- osmfoundation.org URLs (most specific paths first) ------------------
    # Some locale files use https://wiki.osmfoundation.org/wiki/... instead of
    # https://osmfoundation.org/wiki/... — normalise first so the rules below
    # can handle both forms.
    ("https://wiki.osmfoundation.org/wiki/",    "https://osmfoundation.org/wiki/"),
    # Longest/most specific paths must precede their own prefixes.
    # e.g. /wiki/Licence/Attribution_Guidelines before /wiki/Licence
    ("https://osmfoundation.org/wiki/Licence_and_Legal_FAQ/Why_would_I_want_my_contributions_to_be_public_domain",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Contributor_Terms"),
    ("https://osmfoundation.org/wiki/Licence/Attribution_Guidelines",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Attribution"),
    ("https://osmfoundation.org/wiki/Licence/Contributor_Terms",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Contributor_Terms"),
    ("https://osmfoundation.org/wiki/Terms_of_Use",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Terms_of_Use"),
    ("https://osmfoundation.org/wiki/Privacy_Policy",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Site_policies"),
    ("https://osmfoundation.org/wiki/Trademark_Policy",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Site_policies"),
    ("https://osmfoundation.org/wiki/Takedown_procedure",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Contact"),
    ("https://osmfoundation.org/wiki/Working_Groups",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Admin_team"),
    ("https://osmfoundation.org/wiki/Licence",       # shorter path — after the /Licence/* entries
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Copyright"),
    ("https://operations.osmfoundation.org/policies/api/",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Site_policies"),
    ("https://operations.osmfoundation.org/policies/tiles/",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Site_policies"),
    ("https://operations.osmfoundation.org/policies/nominatim/",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Site_policies"),
    ("https://osmfoundation.org/Contact",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Contact"),
    ("https://osmfoundation.org/Licence",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Copyright"),
    ("https://www.osmfoundation.org/",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Admin_team"),
    ("https://osmfoundation.org/",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Admin_team"),
    # -- openstreetmap.org subdomains (before bare domain) -------------------
    ("supporting.openstreetmap.org/donate/",
     "wiki.opengeofiction.net/index.php/OpenGeofiction:Donate"),
    ("dmca.openstreetmap.org",
     "wiki.opengeofiction.net/index.php/OpenGeofiction:Contact"),
    # -- openstreetmap.org wiki --------------------------------------------------
    # Strip language prefixes (DE:, FR:, Zh-hans:, Bs:, etc.) FIRST so the
    # English-page rules below can fire on the normalised URL.
    # Regex matches 2–3 letter base codes (ISO 639) plus optional script subtag,
    # with a negative lookahead to exclude MediaWiki content namespaces (Key:,
    # Tag:, Relation:, Special:, Template:, etc.) which must not be stripped.
    # e.g. .../wiki/Da:Anonymous_edits -> .../wiki/Anonymous_edits (caught below)
    # Pages with translated names (e.g. FR:Guide_du_d%C3%A9butant) fall through
    # to the domain swap and remain as wiki.opengeofiction.net/wiki/<name>.
    (re.compile(r'(https?://wiki\.openstreetmap\.org/wiki/)(?!Key:|Tag:|Relation:|Template:|File:|Help:|Talk:|User:|Special:|Category:)[A-Za-z]{2,3}(?:-[A-Za-z]+)?:'), r'\1'),
    # Special:MyLanguage variants (most-specific, before bare page names)
    ("https://wiki.openstreetmap.org/wiki/Special:MyLanguage/Films",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Attribution#Films"),
    ("https://wiki.openstreetmap.org/wiki/Special:MyLanguage/TV_series",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Attribution#TV_series"),
    # Anchored variants before bare page names
    ("https://wiki.openstreetmap.org/wiki/GPX#Troubleshooting",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Getting_started"),
    # index.php?title= style URLs (old MediaWiki format)
    ("https://wiki.openstreetmap.org/index.php?title=Upload",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Getting_started"),
    # English page names
    ("https://wiki.openstreetmap.org/wiki/About_OpenStreetMap",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:About"),
    ("https://wiki.openstreetmap.org/wiki/Films",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Attribution#Films"),
    ("https://wiki.openstreetmap.org/wiki/TV_series",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Attribution#TV_series"),
    ("https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Site_policies"),
    ("https://wiki.openstreetmap.org/wiki/Acceptable_Use_Policy",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Site_policies"),
    ("https://wiki.openstreetmap.org/wiki/Anonymous_edits",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Site_policies"),
    ("https://wiki.openstreetmap.org/wiki/Import/Guidelines",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Site_policies"),
    ("https://wiki.openstreetmap.org/wiki/Beginners%27_guide",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Getting_started"),
    ("https://wiki.openstreetmap.org/wiki/Beginners_Guide_1.2",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Getting_started"),
    ("https://wiki.openstreetmap.org/wiki/Visibility_of_GPS_traces",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Getting_started"),
    ("https://wiki.openstreetmap.org/wiki/Upload",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Getting_started"),
    ("https://wiki.openstreetmap.org/wiki/GPX_Import_Failures",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Contact"),
    ("https://wiki.openstreetmap.org/wiki/Contact_channels",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Contact"),
    ("https://wiki.openstreetmap.org/wiki/Contact",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Contact"),
    ("https://wiki.openstreetmap.org/wiki/Contributor_Terms_Declined",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Contributor_Terms"),
    ("https://wiki.openstreetmap.org/wiki/Contributors",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Admin_team"),
    ("https://wiki.openstreetmap.org/wiki/User_group",
     "https://wiki.opengeofiction.net/index.php/OpenGeofiction:Admin_team"),
    ("https://wiki.openstreetmap.org/wiki/Gravatar",
     "https://wiki.opengeofiction.net/index.php/Help:Frequently_asked_questions#Gravatar"),
    ("https://wiki.openstreetmap.org/wiki/This_map_requires_WebGL",
     "https://wiki.opengeofiction.net/index.php/Help:Frequently_asked_questions#This_map_requires_WebGL"),
    ("wiki.openstreetmap.org",
     "wiki.opengeofiction.net"),
    # OSM tagging documentation (Key:, Tag:, Relation:) lives on the OSM wiki —
    # OGF uses the same tagging schema. Revert the domain swap for these.
    ("wiki.opengeofiction.net/wiki/Key:",      "wiki.openstreetmap.org/wiki/Key:"),
    ("wiki.opengeofiction.net/wiki/Tag:",      "wiki.openstreetmap.org/wiki/Tag:"),
    ("wiki.opengeofiction.net/wiki/Relation:", "wiki.openstreetmap.org/wiki/Relation:"),
    # community — specific paths before bare domain
    ("https://community.openstreetmap.org/c/communities/dk/77",
     "https://wiki.opengeofiction.net/index.php/Forum:Index"),
    ("https://community.openstreetmap.org/c/communities/ua/66",
     "https://wiki.opengeofiction.net/index.php/Forum:Index"),
    ("community.openstreetmap.org",
     "wiki.opengeofiction.net/index.php/Forum:Index"),
    # blog — locale-specific ?lang= variants before bare domain
    ("https://blog.openstreetmap.org/?lang=cs",    "https://wiki.opengeofiction.net/index.php/Forum:Index"),
    ("https://blog.openstreetmap.org/?lang=de",    "https://wiki.opengeofiction.net/index.php/Forum:Index"),
    ("https://blog.openstreetmap.org/?lang=es",    "https://wiki.opengeofiction.net/index.php/Forum:Index"),
    ("https://blog.openstreetmap.org/?lang=fr",    "https://wiki.opengeofiction.net/index.php/Forum:Index"),
    ("https://blog.openstreetmap.org/?lang=gl",    "https://wiki.opengeofiction.net/index.php/Forum:Index"),
    ("https://blog.openstreetmap.org/?lang=hu",    "https://wiki.opengeofiction.net/index.php/Forum:Index"),
    ("https://blog.openstreetmap.org/?lang=pt-br", "https://wiki.opengeofiction.net/index.php/Forum:Index"),
    ("https://blog.openstreetmap.org/?lang=pt-pt", "https://wiki.opengeofiction.net/index.php/Forum:Index"),
    ("https://blog.openstreetmap.org/?lang=uk",    "https://wiki.opengeofiction.net/index.php/Forum:Index"),
    ("blog.openstreetmap.org",
     "wiki.opengeofiction.net/index.php/Forum:Index"),
    ("blogs.openstreetmap.org",
     "wiki.opengeofiction.net/index.php/Forum:Index"),
    ("lists.openstreetmap.org",
     "wiki.opengeofiction.net/index.php/Forum:Index"),
    ("irc.openstreetmap.org",
     "wiki.opengeofiction.net/index.php/Forum:Index"),
    ("welcome.openstreetmap.org",
     "wiki.opengeofiction.net/index.php/OpenGeofiction:Getting_started"),
    # -- openstreetmap.org bare domain ---------------------------------------
    ("www.openstreetmap.org",       "www.opengeofiction.net"),
    ("openstreetmap.org",           "opengeofiction.net"),
    # -- Compound text phrases (before bare "OpenStreetMap") -----------------
    ("[OpenStreetMap]",             "[OpenGeofiction]"),
    # Foundation name — OGF has no separate legal entity, collapse to the
    # project name.
    ("OpenStreetMap Foundation",    "OpenGeofiction"),
    ("OSM Foundation",              "OpenGeofiction"),
    # "(OSMF)" is a parenthetical abbreviation — drop it entirely (with the
    # preceding space) rather than produce the redundant "(OpenGeofiction)".
    (" (OSMF)",                     ""),
    # Bare OSMF in running text ("contact the OSMF", "OSMF working group")
    ("OSMF",                        "OpenGeofiction"),
    # Specific compound OSM phrases safe to replace
    ("OSM servers",                 "OGF servers"),
    ("OSM contributors",            "OGF contributors"),
    # -- Bare project name ---------------------------------------------------
    ("OpenStreetMap",               "OpenGeofiction"),
    # -- Catch-all for residual wiki.opengeofiction.net/wiki/ URLs ------------
    # The OGF wiki does not use the /wiki/ path prefix (only /index.php/).
    # Any remaining /wiki/ URLs are either translated-name language pages with
    # no OGF equivalent, or other unmapped paths.  Redirect them to the wiki
    # main page.  Key:/Tag:/Relation: links have already been reverted to
    # wiki.openstreetmap.org above, so they are not caught here.
    (re.compile(r'https?://wiki\.opengeofiction\.net/wiki/[^\s"\'<>]+'),
     "https://wiki.opengeofiction.net/"),
]


def should_skip(line: str) -> bool:
    return any(marker in line for marker in SKIP_SUBSTRINGS)


def apply_substitutions(text: str) -> tuple[str, list[str]]:
    """Apply all substitutions to a string. Returns (new_text, list_of_changes)."""
    changes = []
    for old, new in SUBSTITUTIONS:
        if isinstance(old, re.Pattern):
            new_text, count = old.subn(new, text)
            if count:
                text = new_text
                changes.append(f"  /{old.pattern}/ → {new!r}")
        else:
            if old in text:
                text = text.replace(old, new)
                changes.append(f"  {old!r} → {new!r}")
    return text, changes


def process_file(path: Path, dry_run: bool, verbose: bool) -> int:
    """Process a single file with automated substitutions. Returns changed line count."""
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    changed_lines = 0
    new_lines = []
    file_changes = []

    for lineno, line in enumerate(lines, 1):
        if should_skip(line):
            new_lines.append(line)
            continue
        new_line, changes = apply_substitutions(line)
        new_lines.append(new_line)
        if changes:
            changed_lines += 1
            if verbose:
                file_changes.append(f"  line {lineno}: {line.rstrip()!r}")
                file_changes.extend(changes)

    if changed_lines:
        if verbose:
            print(f"\n{path}  ({changed_lines} line(s) changed)")
            print("\n".join(file_changes))
        else:
            print(f"  {path.name}  ({changed_lines} line(s) changed)")

        if not dry_run:
            path.write_text("".join(new_lines), encoding="utf-8")

    return changed_lines


def load_overrides(overrides_path: Path) -> dict:
    """
    Parse etc/applyLocaleSubs.en.yml into a flat {key: raw_block} dict.

    The file is a plain YAML mapping of dot-separated key paths to replacement
    values. We parse it with a simple line scanner rather than a YAML library
    so we preserve exact indentation and multi-line block scalars verbatim —
    the replacement text is spliced directly into the target file.

    Format example (indentation must use spaces, not tabs):
      site.copyright.community_driven_1_html: |
        OpenGeofiction's community is diverse...
      layouts.header.donate: Donate to OGF
    """
    if not overrides_path.exists():
        return {}

    overrides = {}
    text = overrides_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    i = 0
    while i < len(lines):
        line = lines[i]
        # Top-level key: not indented, not a comment, contains ':'
        if line and not line[0].isspace() and not line[0] == "#" and ":" in line:
            colon = line.index(":")
            key = line[:colon].strip()
            rest = line[colon + 1:]
            if rest.strip() == "|":
                # Block scalar — collect content lines, strip their source
                # indentation so apply_overrides can re-indent to the correct
                # depth for the target file.
                content_lines = []
                i += 1
                while i < len(lines) and (not lines[i] or lines[i][0].isspace()):
                    content_lines.append(lines[i])
                    i += 1
                # Find minimum indentation of non-empty content lines
                src_indent = min(
                    (len(l) - len(l.lstrip()) for l in content_lines if l.strip()),
                    default=0,
                )
                stripped_lines = [
                    l[src_indent:] if l.strip() else l
                    for l in content_lines
                ]
                overrides[key] = ("block", "".join(stripped_lines))
            else:
                overrides[key] = ("inline", rest)  # includes leading space
                i += 1
        else:
            i += 1

    return overrides


def apply_overrides(path: Path, overrides: dict, dry_run: bool, verbose: bool) -> int:
    """
    Splice OGF-specific overrides into a locale file by YAML key path.

    Walks the file looking for a key whose dot-joined ancestor path matches
    an override key. When found, replaces the value (including any following
    indented block-scalar lines) with the override text.

    Returns the number of keys replaced.
    """
    if not overrides:
        return 0

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    # Build a stack to track current YAML path based on indentation
    # Each entry: (indent_level, key_fragment)
    indent_stack = []  # list of (indent, key)
    new_lines = []
    replaced = 0
    skip_until_indent = None  # when set, skip lines more-indented than this
    file_changes = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip("\n\r")

        if skip_until_indent is not None:
            # We're consuming the old block value — skip indented continuation lines
            if stripped == "" or (stripped[0].isspace() and
                                  len(stripped) - len(stripped.lstrip()) > skip_until_indent):
                i += 1
                continue
            else:
                skip_until_indent = None

        # Determine indentation of this line
        if stripped and not stripped.lstrip().startswith("#"):
            indent = len(stripped) - len(stripped.lstrip())
            # Pop stack entries that are at same or deeper indent
            while indent_stack and indent_stack[-1][0] >= indent:
                indent_stack.pop()

            # Does this line define a key?
            lstripped = stripped.lstrip()
            if ":" in lstripped:
                colon = lstripped.index(":")
                key_frag = lstripped[:colon].strip().strip('"').strip("'")
                indent_stack.append((indent, key_frag))
                current_path = ".".join(k for _, k in indent_stack)

                if current_path in overrides:
                    kind, override_val = overrides[current_path]
                    prefix = " " * indent + key_frag + ":"
                    if kind == "block":
                        # Re-indent content to key_indent + 2
                        target_indent = " " * (indent + 2)
                        content = "".join(
                            target_indent + l if l.strip() else l
                            for l in override_val.splitlines(keepends=True)
                        )
                        new_lines.append(prefix + " |\n" + content)
                    else:
                        new_lines.append(prefix + override_val)
                    replaced += 1
                    if verbose:
                        file_changes.append(f"  override: {current_path}")
                    # Skip original value lines (block scalar or inline)
                    rest = lstripped[colon + 1:].strip()
                    if rest == "|" or rest == ">":
                        skip_until_indent = indent
                    i += 1
                    continue

        new_lines.append(line)
        i += 1

    if replaced:
        if verbose:
            print(f"\n{path}  ({replaced} override(s) applied)")
            print("\n".join(file_changes))
        else:
            print(f"  {path.name}  ({replaced} override(s) applied)")

        if not dry_run:
            path.write_text("".join(new_lines), encoding="utf-8")

    return replaced


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Localise OSM references for OpenGeofiction."
    )
    parser.add_argument(
        "repo_root",
        help="Path to the openstreetmap-website repository root",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would change without writing files",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show each changed line and the substitution applied",
    )
    args = parser.parse_args()

    repo = Path(args.repo_root)
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        sys.exit(1)

    locales_path = repo / "config" / "locales"
    if not locales_path.is_dir():
        print(f"error: {locales_path} not found — is this the repo root?", file=sys.stderr)
        sys.exit(1)

    # Load OGF-specific en.yml overrides from etc/ sibling of this script
    overrides_path = Path(__file__).parent.parent / "etc" / "applyLocaleSubs.en.yml"
    overrides = load_overrides(overrides_path)
    if overrides:
        print(f"Loaded {len(overrides)} override(s) from {overrides_path.name}")
    elif not overrides_path.exists():
        print(f"note: no overrides file at {overrides_path}", file=sys.stderr)

    # Locale files — all *.yml in config/locales/
    files_to_process = sorted(locales_path.glob("*.yml"))
    if not files_to_process:
        print(f"error: no .yml files found in {locales_path}", file=sys.stderr)
        sys.exit(1)

    # Additional config files that also contain OSM references
    # (settings.yml is intentionally excluded — OGF overrides go in settings.local.yml)
    EXTRA_FILES = []
    for rel in EXTRA_FILES:
        p = repo / rel
        if p.exists():
            files_to_process.append(p)
        else:
            print(f"warning: {rel} not found, skipping", file=sys.stderr)

    if args.dry_run:
        print("DRY RUN — no files will be written\n")

    total_files = 0
    total_lines = 0

    en_yml = locales_path / "en.yml"
    total_overrides = 0

    for path in files_to_process:
        changed = process_file(path, dry_run=args.dry_run, verbose=args.verbose)
        if changed:
            total_files += 1
            total_lines += changed

    # Apply OGF-specific overrides to en.yml after substitutions
    if overrides and en_yml.exists():
        replaced = apply_overrides(en_yml, overrides, dry_run=args.dry_run, verbose=args.verbose)
        total_overrides += replaced

    verb = "Would change" if args.dry_run else "Changed"
    print(f"\n{verb} {total_lines} line(s) across {total_files} file(s).", end="")
    if total_overrides:
        print(f" Applied {total_overrides} override(s) to en.yml.", end="")
    print()


if __name__ == "__main__":
    main()
