#!/usr/bin/env python3
"""Check whether arXiv preprints listed in the living review have been published.

Parses README.md for arXiv entries, queries the arXiv API for journal
references / DOIs, and optionally rewrites the affected table rows.

Usage:
    python3 scripts/check_arxiv.py            # report mode, exit 1 if updates needed
    python3 scripts/check_arxiv.py --apply    # rewrite README.md rows in place
    python3 scripts/check_arxiv.py --file other.md
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

README = "README.md"
ARXIV_API = "https://export.arxiv.org/api/query?id_list={ids}&max_results={n}"
CROSSREF_API = "https://api.crossref.org/works/{doi}"
USER_AGENT = "ocean-sea-ice-emulators-living-review/1.0 (https://github.com/IGE-OPERA/ocean-sea-ice-emulators-living-review)"

# Journal short names used in the review, keyed by plausible Crossref
# container titles. Matched case-insensitively on substring.
JOURNAL_ALIASES = [
    ("geophysical research letters", "GRL"),
    ("journal of advances in modeling earth systems", "JAMES"),
    ("science advances", "Science Advances"),
    ("nature communications", "Nature Communications"),
    ("scientific reports", "Nature Scientific Reports"),
    ("artificial intelligence for the earth systems", "Artificial Intelligence for the Earth Systems"),
    (" agu advances", "AGU Advances"),
    ("earth system science data", "Earth System Science Data"),
    ("ocean modelling", "Ocean Modelling"),
    ("journal of climate", "Journal of Climate"),
    ("climate dynamics", "Climate Dynamics"),
    ("environmental research letters", "Environmental Research Letters"),
    ("nature machine intelligence", "Nature Machine Intelligence"),
    ("nature", "Nature"),
    ("communications earth", "Communications Earth"),
    (" npj climate", "npj Climate and Atmospheric Science"),
    ("advances in atmospheric sciences", "Advances in Atmospheric Sciences"),
    ("the cryosphere", "The Cryosphere"),
    ("annual review of marine science", "Annual Review of Marine Science"),
    ("monthly weather review", "Monthly Weather Review"),
    ("weather and forecasting", "Weather and Forecasting"),
    ("journal of advances", "JAMES"),
]

ARXIV_DOI_RE = re.compile(r"10\.48550/arXiv\.(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
ABS_DOI_RE = re.compile(r"arxiv\.org/abs/(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
ROW_RE = re.compile(r"^\|(.+)\|$")
CELL_SPLIT_RE = re.compile(r"\s*\|\s*")

ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_rows(text: str):
    """Return list of dicts describing arXiv rows found in markdown tables."""
    rows = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped.startswith("|"):
            continue
        m = ARXIV_DOI_RE.search(stripped) or ABS_DOI_RE.search(stripped)
        if not m:
            continue
        arxiv_id = m.group(1)
        # Skip rows that already point to a journal DOI (no arXiv link remains
        # as the primary paper link) - the regex above only matches arXiv links.
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        rows.append(
            {
                "lineno": lineno,
                "arxiv_id": arxiv_id,
                "cells": cells,
                "line": stripped,
            }
        )
    return rows


def fetch_arxiv(ids):
    """Query the arXiv API. Returns {arxiv_id: {'doi':..., 'journal_ref':..., 'title':...}}."""
    results = {}
    # arXiv asks for batches of at most ~20 ids and 3s between calls.
    for i in range(0, len(ids), 20):
        batch = ids[i : i + 20]
        url = ARXIV_API.format(ids=",".join(batch), n=len(batch))
        data = http_get(url)
        root = ET.fromstring(data)
        for entry in root.findall("atom:entry", ARXIV_NS):
            id_el = entry.find("atom:id", ARXIV_NS)
            title_el = entry.find("atom:title", ARXIV_NS)
            doi_el = entry.find("arxiv:doi", ARXIV_NS)
            jref_el = entry.find("arxiv:journal_ref", ARXIV_NS)
            if id_el is None or not id_el.text:
                continue
            m = re.search(r"abs/([^v]+)(v\d+)?$", id_el.text.strip())
            if not m:
                continue
            arxiv_id = m.group(1)
            results[arxiv_id] = {
                "doi": doi_el.text.strip() if doi_el is not None and doi_el.text else None,
                "journal_ref": jref_el.text.strip() if jref_el is not None and jref_el.text else None,
                "title": re.sub(r"\s+", " ", title_el.text).strip() if title_el is not None else "",
            }
        if i + 20 < len(ids):
            time.sleep(3)
    return results


def fetch_crossref(doi: str):
    """Fetch Crossref metadata for a journal DOI. Returns dict or None."""
    try:
        data = json.loads(http_get(CROSSREF_API.format(doi=doi)).decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    msg = data.get("message", {})
    title = msg.get("container-title") or []
    parts = msg.get("issued", {}).get("date-parts", [[None]])
    year = parts[0][0] if parts and parts[0] else None
    return {"journal": title[0] if title else None, "year": year}


def journal_short_name(container_title: str) -> str:
    if not container_title:
        return "journal publication"
    low = container_title.lower()
    for needle, short in JOURNAL_ALIASES:
        if needle.strip() in low:
            return short
    return container_title.strip()


def build_new_row(row, pub):
    """Construct the replacement table row for a published entry."""
    cells = list(row["cells"])
    year = pub.get("year") or cells[0]
    cells[0] = str(year)
    cells[2] = pub["journal_short"]
    cells[3] = f'[doi](https://doi.org/{pub["doi"]})'
    return "| " + " | ".join(cells) + " |"


def normalize_row_widths(text: str, changed_lines: set):
    """Re-align table columns for tables containing modified rows.

    Only tables that have at least one changed line are touched. Within each
    such table, every cell is padded to the new column width so the table
    stays aligned in monospace rendering. Separator rows are regenerated.
    """
    lines = text.splitlines()
    # Group contiguous table blocks and note which contain changes.
    blocks = []
    current = []
    for i, line in enumerate(lines):
        if line.strip().startswith("|"):
            current.append(i)
        else:
            if current:
                blocks.append(current)
                current = []
    if current:
        blocks.append(current)

    for block in blocks:
        if not any(i in changed_lines for i in block):
            continue
        parsed = []
        for i in block:
            stripped = lines[i].strip()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            parsed.append((i, cells))
        if len({len(c) for _, c in parsed}) != 1:
            continue  # ragged block, leave untouched
        widths = [0] * len(parsed[0][1])
        for _, cells in parsed:
            for j, c in enumerate(cells):
                widths[j] = max(widths[j], len(c))
        for i, cells in parsed:
            if all(set(c) <= {"-", ":"} and c for c in cells):
                lines[i] = "|" + "|".join("-" * (widths[j] + 2) for j in range(len(cells))) + "|"
            else:
                lines[i] = "| " + " | ".join(c.ljust(widths[j]) for j, c in enumerate(cells)) + " |"
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=README, help=f"markdown file to check (default: {README})")
    parser.add_argument("--apply", action="store_true", help="rewrite rows in place instead of only reporting")
    args = parser.parse_args()

    try:
        text = open(args.file, encoding="utf-8").read()
    except OSError as e:
        print(f"error: cannot read {args.file}: {e}", file=sys.stderr)
        return 2

    rows = parse_rows(text)
    if not rows:
        print("No arXiv entries found in", args.file)
        return 0

    print(f"Found {len(rows)} arXiv entries; querying arXiv API...")
    ids = []
    seen = set()
    for r in rows:
        if r["arxiv_id"] not in seen:
            seen.add(r["arxiv_id"])
            ids.append(r["arxiv_id"])
    try:
        meta = fetch_arxiv(ids)
    except (urllib.error.URLError, ET.ParseError) as e:
        print(f"error: arXiv API request failed: {e}", file=sys.stderr)
        return 2

    published = []
    for row in rows:
        info = meta.get(row["arxiv_id"])
        if not info or not (info["doi"] or info["journal_ref"]):
            continue
        doi = info["doi"]
        pub = {"arxiv_id": row["arxiv_id"], "journal_ref": info["journal_ref"], "doi": doi, "title": info["title"]}
        if doi:
            cr = fetch_crossref(doi)
            if cr:
                pub["year"] = cr["year"]
                pub["journal_short"] = journal_short_name(cr["journal"])
            else:
                pub["year"] = None
                pub["journal_short"] = journal_short_name(info["journal_ref"]) if info["journal_ref"] else "journal publication"
        else:
            pub["year"] = None
            pub["journal_short"] = journal_short_name(info["journal_ref"]) if info["journal_ref"] else "journal publication"
        pub["old_row"] = row["line"]
        pub["new_row"] = build_new_row(row, pub)
        pub["lineno"] = row["lineno"]
        published.append(pub)

    if not published:
        print("All arXiv preprints are still unpublished. No updates needed.")
        return 0

    print(f"\n{len(published)} entr{'y is' if len(published) == 1 else 'ies are'} now published:\n")
    print("| arXiv | Title | Journal | DOI |")
    print("|-------|-------|---------|-----|")
    for p in published:
        print(f"| arXiv:{p['arxiv_id']} | {p['title'][:60]} | {p['journal_short']} | {p['doi']} |")
    print()
    for p in published:
        print(f"### arXiv:{p['arxiv_id']} — {p['title']}")
        print(f"- Old row: `{p['old_row']}`")
        print(f"- New row: `{p['new_row']}`\n")

    if args.apply:
        lines = text.splitlines()
        changed = set()
        for p in published:
            lines[p["lineno"] - 1] = p["new_row"]
            changed.add(p["lineno"] - 1)
        new_text = normalize_row_widths("\n".join(lines) + ("\n" if text.endswith("\n") else ""), changed)
        with open(args.file, "w", encoding="utf-8") as f:
            f.write(new_text)
        print(f"Applied {len(published)} update(s) to {args.file}.")
    else:
        print(f"Run with --apply to update {args.file} automatically.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
