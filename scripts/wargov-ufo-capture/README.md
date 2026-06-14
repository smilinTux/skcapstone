# war.gov/UFO/ PURSUE capture scripts

Lumina-built CDP scripts for capturing war.gov/UFO/ PURSUE releases via the Lumina Chrome browser harness (port 9222). Akamai TLS-fingerprint gates direct curl, so capture has to drive a real browser session.

## Quick reference

| Script | What it does |
|---|---|
| `cdp_probe.py` | Open a tab on `war.gov/UFO/`, inspect the page meta + inline scripts to find the current CSV URL and any new release ZIP bundle. Run this first when a new release lands. |
| `cdp_capture_release2.py` | Reference implementation of a full-release capture. Pulls the new CSV, downloads the ZIP bundle, and saves the press-release HTML. Per-release: edit `ZIP_URL`, `CSV_URL`, `PRESS_URL`, `DOC_DIR` constants. |
| `parse_csv.py` | Parses the merged `uap-data.csv`, splits records by `Release Date`, produces `release-02-records.json` + `release-02-urls.json`. Run after CSV fetch. |
| `pull_dvids.sh` | xargs-parallel-4 batch DVIDS puller. Reads `release-02-records.json`, fetches each DVIDS page, greps mp4 CDN URL, downloads. **Note:** DVIDS classifies UAP audio records as `/video/<id>` pages — always use `/video/` prefix, NOT `/audio/`. |
| `cdp_finish.py` | Tail-end CDP script: re-extract press release `innerText`, page-context-fetch the 6 thumbnails (Akamai-gated to curl), and grab FBI Vault Part 15. |

## Capture flow for a new release (Release 03 etc.)

1. Confirm Lumina Chrome is up at :9222 (`~/bin/lumina-x-browser` to start).
2. `python3 cdp_probe.py` — confirms the data source URL and looks for a ZIP-bundle path.
3. Edit `cdp_capture_release2.py` constants for the new release: `ZIP_URL`, `CSV_URL`, `PRESS_URL`, `DOC_DIR`.
4. `python3 cdp_capture_release2.py` — pulls CSV + ZIP + press-release HTML.
5. `python3 parse_csv.py` — generates per-record inventory + URL set + DVIDS-only list.
6. `bash pull_dvids.sh` — pulls all DVIDS media in parallel-4.
7. `python3 cdp_finish.py` — re-extract press text (it picks the wrong selector on first pass), thumbnails, and any FBI Vault references.
8. Update README.md at `~/nextcloud/cbrd21-share/reference/war-gov-UFO-PURSUE-2026/`.

## Gotchas

- **DVIDS audio → use `/video/` URL.** "Audio" UAP records are served from `dvidshub.net/video/<id>` with a static image. The `/audio/` path returns 404 for these IDs.
- **Some DVIDS IDs are shared across records.** E.g., Release 02 ID `1007720` covers both DOW-UAP-PR057a and DOW-UAP-PR057b. Inventory may show N records but only N-1 unique IDs.
- **Akamai bm tokens are per-session.** Cookies extracted from the browser will NOT work with curl — Akamai checks TLS fingerprint, not cookies. Stay in the browser.
- **CSV name can change.** Release 01 used `uap-csv.csv`; when Release 02 shipped, it was renamed to `uap-data.csv`. Re-probe the inline scripts to find the current name.
- **The ZIP bundle is PDFs only.** Don't skip DVIDS pulls just because you got the ZIP — videos/audio are not in the bundle.

## Naming convention

Match Release 01: `dvids-<id>-<UAP-PR-code>.mp4` (e.g. `dvids-1007706-DOW-UAP-PR050.mp4`). Cleaner than verbose slugs and matches existing on-disk corpus.

## Output location

`~/nextcloud/cbrd21-share/reference/war-gov-UFO-PURSUE-2026/`
- `release-NN/` for media
- `docs/release-NN/` for press release + CSV + manifest + thumbnails
- `release-NN-zip/` for the official ZIP bundle if provided
