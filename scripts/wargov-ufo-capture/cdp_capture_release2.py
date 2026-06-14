#!/usr/bin/env python3
"""Capture war.gov/UFO/ Release 02 via Lumina Chrome CDP.

Strategy (Release 02 is bundled into one ZIP, plus a fresh CSV + press release):
  1. Open a tab on war.gov/UFO/ to seed Akamai cookies in the Chrome session.
  2. Set Page.setDownloadBehavior to allow downloads to our target dir.
  3. Trigger ZIP download by injecting <a download href=...> and clicking it.
  4. Poll for .crdownload to drain and the final file to appear.
  5. Also fetch the new CSV in-page (text response — simpler than download).
  6. Fetch the press release HTML the same way.

Output → ~/nextcloud/cbrd21-share/reference/war-gov-UFO-PURSUE-2026/{docs/release-02, release-02-zip}/
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

import websocket

CDP_HTTP = "http://127.0.0.1:9222"
SEED_URL = "https://www.war.gov/UFO/"

ZIP_URL = "https://www.war.gov/medialink/ufo/052226/release_02/release_02_document_bundle.zip"
CSV_URL = "https://www.war.gov/Portals/1/Interactive/2026/UFO/uap-data.csv"
PRESS_URL = "https://www.war.gov/News/Releases/Release/Article/4499305/department-of-war-publishes-second-release-of-unidentified-anomalous-phenomena/"

BASE = Path("/home/cbrd21/nextcloud/cbrd21-share/reference/war-gov-UFO-PURSUE-2026")
DOC_DIR = BASE / "docs" / "release-02"
ZIP_DIR = BASE / "release-02-zip"
DOC_DIR.mkdir(parents=True, exist_ok=True)
ZIP_DIR.mkdir(parents=True, exist_ok=True)


def cdp_get(path: str) -> dict | list:
    with urllib.request.urlopen(f"{CDP_HTTP}{path}") as r:
        return json.loads(r.read())


def open_tab(url: str) -> dict:
    req = urllib.request.Request(f"{CDP_HTTP}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def close_tab(target_id: str) -> None:
    try:
        with urllib.request.urlopen(f"{CDP_HTTP}/json/close/{target_id}", timeout=5):
            pass
    except Exception:
        pass


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=120)
        self.mid = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        self.mid += 1
        msg_id = self.mid
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        self.ws.settimeout(timeout)
        while True:
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def wait_event(self, name: str, timeout: float = 30.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.1, deadline - time.time()))
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            msg = json.loads(raw)
            if msg.get("method") == name:
                return msg.get("params", {})
        raise TimeoutError(f"event {name} did not fire within {timeout}s")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def fetch_text_in_page(cdp: CDP, url: str) -> tuple[int, str]:
    expr = (
        f"(async () => {{"
        f"  const r = await fetch({json.dumps(url)}, {{credentials: 'include', cache: 'no-store'}});"
        f"  return {{status: r.status, text: await r.text()}};"
        f"}})()"
    )
    res = cdp.call("Runtime.evaluate", {
        "expression": expr,
        "awaitPromise": True,
        "returnByValue": True,
    }, timeout=180)
    val = res.get("result", {}).get("value", {}) or {}
    return val.get("status", 0), val.get("text", "")


def trigger_download(cdp: CDP, url: str) -> None:
    expr = (
        f"(() => {{"
        f"  const a = document.createElement('a');"
        f"  a.href = {json.dumps(url)};"
        f"  a.download = '';"
        f"  document.body.appendChild(a);"
        f"  a.click();"
        f"  a.remove();"
        f"  return 'click-triggered';"
        f"}})()"
    )
    cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})


def wait_for_file(path: Path, partial_glob: str, timeout: float = 1800.0, idle_threshold: float = 5.0) -> Path | None:
    """Wait until a file matching the final name shows up + a quiet period after .crdownload drains."""
    deadline = time.time() + timeout
    last_size = -1
    last_change = time.time()
    while time.time() < deadline:
        # Find .crdownload first
        crfiles = list(path.glob("*.crdownload"))
        finished = [p for p in path.glob(partial_glob) if not p.name.endswith(".crdownload")]
        if crfiles:
            size = sum(f.stat().st_size for f in crfiles)
            if size != last_size:
                last_size = size
                last_change = time.time()
                print(f"[download] in-progress {size/1e6:.1f} MB", flush=True)
            time.sleep(2.0)
        elif finished:
            # No crdownload, file is there. Need idle period to ensure stable.
            f = finished[0]
            size = f.stat().st_size
            if size != last_size:
                last_size = size
                last_change = time.time()
            if time.time() - last_change >= idle_threshold:
                return f
            time.sleep(1.0)
        else:
            time.sleep(2.0)
    return None


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    print(f"[capture] seeding tab → {SEED_URL}", flush=True)
    tab = open_tab(SEED_URL)
    target_id = tab["id"]
    ws_url = tab["webSocketDebuggerUrl"]
    cdp = CDP(ws_url)
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Network.enable", {"maxPostDataSize": 0})
        cdp.call("Page.navigate", {"url": SEED_URL})
        try:
            cdp.wait_event("Page.loadEventFired", timeout=30.0)
        except TimeoutError:
            pass
        time.sleep(3.0)  # let Vue settle and cookies stick

        # ---- 1. Fetch CSV (small, text)
        print(f"[capture] fetching CSV → {CSV_URL}", flush=True)
        status, text = fetch_text_in_page(cdp, CSV_URL)
        print(f"[capture] CSV status={status} len={len(text)}", flush=True)
        if status == 200 and text:
            (DOC_DIR / "uap-data.csv").write_text(text)
        else:
            (DOC_DIR / "uap-data-error.json").write_text(json.dumps({"status": status, "preview": text[:1000]}, indent=2))

        # ---- 2. Fetch press release HTML
        print(f"[capture] fetching press release → {PRESS_URL}", flush=True)
        status, text = fetch_text_in_page(cdp, PRESS_URL)
        print(f"[capture] press release status={status} len={len(text)}", flush=True)
        if status == 200 and text:
            (DOC_DIR / "press-release-2026-05-22.html").write_text(text)
            # Try to extract clean text via DOM
            txt_expr = (
                f"(async () => {{"
                f"  const r = await fetch({json.dumps(PRESS_URL)}, {{credentials: 'include'}});"
                f"  const html = await r.text();"
                f"  const doc = new DOMParser().parseFromString(html, 'text/html');"
                f"  const article = doc.querySelector('.body-text') || doc.querySelector('article') || doc.querySelector('main') || doc.body;"
                f"  return article ? article.innerText : '';"
                f"}})()"
            )
            res = cdp.call("Runtime.evaluate", {
                "expression": txt_expr,
                "awaitPromise": True,
                "returnByValue": True,
            }, timeout=60)
            article_text = res.get("result", {}).get("value", "") or ""
            if article_text:
                (DOC_DIR / "press-release-2026-05-22.txt").write_text(article_text)
                print(f"[capture] extracted {len(article_text)} chars of article text", flush=True)

        # ---- 3. Download the ZIP bundle via download behavior + <a download> click
        print(f"[capture] setting download dir → {ZIP_DIR}", flush=True)
        cdp.call("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": str(ZIP_DIR),
        })
        # Also try Browser.setDownloadBehavior which is the newer API
        try:
            cdp.call("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": str(ZIP_DIR),
                "eventsEnabled": True,
            })
        except Exception as e:
            print(f"[capture] Browser.setDownloadBehavior not supported: {e}", flush=True)

        print(f"[capture] triggering ZIP download → {ZIP_URL}", flush=True)
        trigger_download(cdp, ZIP_URL)

        # Poll for completion
        zip_file = wait_for_file(ZIP_DIR, "release_02_document_bundle*.zip", timeout=1800.0, idle_threshold=5.0)
        if not zip_file:
            print("[capture] ZIP download did NOT complete in 30 min — check ZIP_DIR manually", flush=True)
            # Diagnostic: list what's in there
            for f in ZIP_DIR.iterdir():
                print(f"  {f.name} {f.stat().st_size}", flush=True)
            return 2

        size_mb = zip_file.stat().st_size / 1e6
        sha = sha256_file(zip_file)
        print(f"[capture] ZIP done: {zip_file.name} {size_mb:.1f} MB sha256={sha}", flush=True)

        # Write manifest
        manifest = {
            "release": "02",
            "release_date": "2026-05-22",
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "zip_url": ZIP_URL,
            "zip_path": str(zip_file),
            "zip_size_bytes": zip_file.stat().st_size,
            "zip_sha256": sha,
            "csv_url": CSV_URL,
            "press_url": PRESS_URL,
            "capture_method": "Lumina Chrome CDP (port 9222) — page-context fetch + <a download> click",
        }
        (DOC_DIR / "release-02-manifest.json").write_text(json.dumps(manifest, indent=2))
        print(f"[capture] manifest written", flush=True)

        return 0
    finally:
        cdp.close()
        close_tab(target_id)


if __name__ == "__main__":
    sys.exit(main())
