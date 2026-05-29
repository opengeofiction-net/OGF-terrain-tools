#!/usr/bin/env python3
"""
find-bare-urls.py - Scan a MediaWiki XML dump for pages containing bare
opengeofiction.net or openstreetmap.org URLs (not inside {{...}} templates).

Outputs a TSV with: count  title  namespace  sample_urls
"""

import gzip
import re
import sys
import xml.etree.ElementTree as ET


NS_URI = "http://www.mediawiki.org/xml/export-0.11/"

# Domains to search for
URL_DOMAINS = re.compile(
    r"https?://(?:www\.)?(?:opengeofiction\.net|openstreetmap\.org)/"
    r"[^\s\]<>}]+"
)


def tag_name(elem):
    """Strip namespace prefix from tag."""
    if elem.tag.startswith("{"):
        return elem.tag.split("}", 1)[1]
    return elem.tag


def iter_pages(xml_path):
    """Stream pages from a MediaWiki XML dump."""
    nsmap = {}  # namespace id -> name

    with gzip.open(xml_path, "rb") as fh:
        context = ET.iterparse(fh, events=("start", "end"))
        _, root = next(context)  # <mediawiki>

        for event, elem in context:
            name = tag_name(elem)

            if event == "end" and name == "namespace":
                key = elem.get("key")
                nsmap[int(key)] = elem.text or ""
                root.clear()

            elif event == "end" and name == "page":
                title_elem = elem.find(f"{{{NS_URI}}}title")
                ns_elem = elem.find(f"{{{NS_URI}}}ns")
                revision = elem.find(f"{{{NS_URI}}}revision")
                if title_elem is None or ns_elem is None or revision is None:
                    root.clear()
                    continue

                title = title_elem.text or ""
                ns_id = int(ns_elem.text or "0")
                ns_name = nsmap.get(ns_id, str(ns_id))

                text_elem = revision.find(f"{{{NS_URI}}}text")
                if text_elem is not None and text_elem.text:
                    yield title, ns_name, text_elem.text

                root.clear()


def find_bare_urls(text):
    """Find all bare OGF/OSM URLs not inside {{...}} template spans."""
    # Split on {{ and }} to isolate template content
    # We track depth: inside a template, URLs should be ignored
    seen = set()
    urls = []

    # Simple approach: find all URLs, then filter out those inside templates
    for m in URL_DOMAINS.finditer(text):
        url = m.group(0)
        if url in seen:
            continue
        # Check if this URL is inside a {{...}} span
        before = text[:m.start()]
        open_count = before.count("{{") - before.count("}}")
        # Also handle {{!}} and other edge cases — but {{ and }} are
        # the main delimiters.  open_count > 0 means we're inside a template.
        if open_count > 0:
            continue
        seen.add(url)
        urls.append(url)

    return urls


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <backup.xml.gz>", file=sys.stderr)
        sys.exit(1)

    xml_path = sys.argv[1]
    results = []

    for title, ns_name, text in iter_pages(xml_path):
        urls = find_bare_urls(text)
        if urls:
            sample = "; ".join(urls[:3])
            if len(urls) > 3:
                sample += f" ... (+{len(urls) - 3} more)"
            results.append((len(urls), title, ns_name, sample))

    # Sort by count descending
    results.sort(key=lambda r: r[0], reverse=True)

    print("count\ttitle\tnamespace\tsample_urls")
    for count, title, ns_name, sample in results:
        # Escape any tabs in the sample
        sample_clean = sample.replace("\t", " ")
        print(f"{count}\t{title}\t{ns_name}\t{sample_clean}")

    total_pages = len(results)
    total_urls = sum(r[0] for r in results)
    print(f"\n# {total_pages} pages with {total_urls} total bare URLs", file=sys.stderr)


if __name__ == "__main__":
    main()
