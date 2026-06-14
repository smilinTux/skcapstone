#!/usr/bin/env python3
"""Parse the new uap-data.csv and split Release 01 vs Release 02 records.

The CSV has multi-line quoted fields (newlines inside Title and Description Blurb),
so we use Python's csv module rather than naive line counting.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

CSV_PATH = Path("/home/cbrd21/nextcloud/cbrd21-share/reference/war-gov-UFO-PURSUE-2026/docs/release-02/uap-data.csv")
OUT_DIR = Path("/home/cbrd21/clawd/tmp/wargov-capture/probe-out")
OUT_DIR.mkdir(parents=True, exist_ok=True)

with CSV_PATH.open(newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    rows = [r for r in reader]

print(f"Total records: {len(rows)}")

date_counter = Counter()
for r in rows:
    date_counter[(r.get("Release Date") or "").strip()] += 1
print("Release dates:")
for d, c in sorted(date_counter.items(), key=lambda x: -x[1]):
    print(f"  {d!r:15} → {c}")

# Filter for Release 02
release2 = [r for r in rows if (r.get("Release Date") or "").strip() == "5/22/26"]
print(f"\nRelease 02 records: {len(release2)}")

# Bucket by type
type_counter = Counter()
agency_counter = Counter()
for r in release2:
    type_counter[(r.get("Type") or "").strip()] += 1
    agency_counter[(r.get("Agency") or "").strip()] += 1
print("Types:")
for t, c in type_counter.most_common():
    print(f"  {t!r:15} → {c}")
print("Agencies:")
for a, c in agency_counter.most_common():
    print(f"  {a!r:15} → {c}")

# Extract download links
links = []
for r in release2:
    pdf_link = (r.get("PDF | Image Link") or "").strip()
    modal = (r.get("Modal Image") or "").strip()
    dvids = (r.get("DVIDS Video ID") or "").strip()
    title = (r.get("Title") or "").strip().replace("\n", " ").replace("\r", "")
    rtype = (r.get("Type") or "").strip()
    agency = (r.get("Agency") or "").strip()
    incident_date = (r.get("Incident Date") or "").strip()
    incident_loc = (r.get("Incident Location") or "").strip()
    links.append({
        "title": title,
        "type": rtype,
        "agency": agency,
        "incident_date": incident_date,
        "incident_location": incident_loc,
        "pdf_link": pdf_link,
        "modal_image": modal,
        "dvids_id": dvids,
    })

# Save full inventory
(OUT_DIR / "release-02-records.json").write_text(json.dumps(links, indent=2))
print(f"\nSaved inventory: {OUT_DIR / 'release-02-records.json'}")

# Unique direct-fetchable URLs
urls = set()
for L in links:
    if L["pdf_link"]:
        urls.add(L["pdf_link"])
    if L["modal_image"]:
        urls.add(L["modal_image"])
urls_list = sorted(urls)
print(f"\nUnique direct URLs: {len(urls_list)}")
for u in urls_list[:15]:
    print(f"  {u}")
if len(urls_list) > 15:
    print(f"  ... and {len(urls_list) - 15} more")

(OUT_DIR / "release-02-urls.json").write_text(json.dumps(urls_list, indent=2))

# DVIDS-only records (videos hosted exclusively on DVIDS)
dvids_only = [L for L in links if L["dvids_id"] and not L["pdf_link"]]
print(f"\nDVIDS-only video records: {len(dvids_only)}")
for L in dvids_only[:10]:
    print(f"  DVIDS {L['dvids_id']}: {L['title'][:80]}")
(OUT_DIR / "release-02-dvids.json").write_text(json.dumps(dvids_only, indent=2))
