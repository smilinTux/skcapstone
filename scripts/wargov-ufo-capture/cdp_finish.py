#!/usr/bin/env python3
"""CDP run 2: re-extract press release text, pull thumbnails, grab FBI Vault Part 15.

Three sub-tasks:
  A. Re-render the press release in a Chrome tab and pull the article-body innerText.
  B. Page-context-fetch the 6 thumbnail JPGs for Release 02.
  C. Navigate to FBI Vault and pull Part 15 of 16 from the 62-HQ-83894 series.
"""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

import websocket

CDP_HTTP = "http://127.0.0.1:9222"

PRESS_URL = "https://www.war.gov/News/Releases/Release/Article/4499305/department-of-war-publishes-second-release-of-unidentified-anomalous-phenomena/"
THUMB_BASE = "https://www.war.gov/medialink/ufo/052226/release_02/thumbnails"
THUMB_NAMES = [
    "CIA-UAP-D001_Intelligence_Information_Report_USSR_1973",
    "DOE-UAP-D001_PANTEX_Image",
    "DOE-UAP-D002_JamesTuck_Correspondence",
    "DOE-UAP-D003_Pajarito_Astronomers",
    "DOW-UAP-D017_General_Correspondence_Of_Sandia",
    "ODNI-UAP-D001_USPER_Narrative_Senior_USIC",
]

FBI_VAULT_BASE = "https://vault.fbi.gov"
# FBI Vault organizes the 62-HQ-83894 UFO file as "Unidentified Flying Objects (UFO)" — Part X of Y
# Known canonical layout has Parts 1-16. Tweet referenced Part 15.
FBI_PART_PAGE = "https://vault.fbi.gov/UFO/UFO%20Part%2015%20of%2016/view"
FBI_PART_PDF_GUESS = "https://vault.fbi.gov/UFO/UFO%20Part%2015%20of%2016/at_download/file"

BASE = Path("/home/cbrd21/nextcloud/cbrd21-share/reference/war-gov-UFO-PURSUE-2026")
DOC_DIR = BASE / "docs" / "release-02"
THUMB_DIR = DOC_DIR / "thumbnails"
THUMB_DIR.mkdir(parents=True, exist_ok=True)

FBI_DIR = Path("/home/cbrd21/nextcloud/cbrd21-share/reference/fbi-vault-ufo-62-HQ-83894")
FBI_DIR.mkdir(parents=True, exist_ok=True)


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


def fetch_binary_in_page(cdp: CDP, url: str) -> tuple[int, bytes | None]:
    """Fetch a binary resource in page context and return as bytes."""
    expr = (
        f"(async () => {{"
        f"  const r = await fetch({json.dumps(url)}, {{credentials: 'include', cache: 'no-store'}});"
        f"  if (!r.ok) return {{status: r.status, b64: null}};"
        f"  const buf = await r.arrayBuffer();"
        f"  const bytes = new Uint8Array(buf);"
        f"  let bin = '';"
        f"  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);"
        f"  return {{status: r.status, b64: btoa(bin), bytes: bytes.length}};"
        f"}})()"
    )
    res = cdp.call("Runtime.evaluate", {
        "expression": expr,
        "awaitPromise": True,
        "returnByValue": True,
    }, timeout=300)
    val = res.get("result", {}).get("value", {}) or {}
    status = val.get("status", 0)
    b64 = val.get("b64")
    if status == 200 and b64:
        return status, base64.b64decode(b64)
    return status, None


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
    }, timeout=120)
    val = res.get("result", {}).get("value", {}) or {}
    return val.get("status", 0), val.get("text", "")


def task_a_press_release(cdp: CDP) -> None:
    """Navigate to press release, extract innerText from main article."""
    print(f"[A] navigating → press release", flush=True)
    cdp.call("Page.navigate", {"url": PRESS_URL})
    try:
        cdp.wait_event("Page.loadEventFired", timeout=30.0)
    except TimeoutError:
        pass
    time.sleep(3.0)

    # Try multiple candidate selectors; press releases on DoW use various article wrappers
    extract_js = (
        "(() => {"
        "  const candidates = ["
        "    document.querySelector('.body-text'),"
        "    document.querySelector('.article-body'),"
        "    document.querySelector('.article-content'),"
        "    document.querySelector('.press-release'),"
        "    document.querySelector('main article'),"
        "    document.querySelector('main .content'),"
        "    document.querySelector('main'),"
        "    document.querySelector('article'),"
        "  ];"
        "  for (const el of candidates) {"
        "    if (el && el.innerText && el.innerText.length > 500) {"
        "      return {selector: el.tagName + (el.className ? '.' + el.className.split(' ').join('.') : ''), text: el.innerText, len: el.innerText.length};"
        "    }"
        "  }"
        "  // Last resort: full body innerText"
        "  return {selector: 'body', text: document.body.innerText, len: document.body.innerText.length};"
        "})()"
    )
    res = cdp.call("Runtime.evaluate", {"expression": extract_js, "returnByValue": True})
    val = res.get("result", {}).get("value", {}) or {}
    text = val.get("text", "")
    print(f"[A] selector={val.get('selector')!r} len={val.get('len')}", flush=True)
    if text:
        (DOC_DIR / "press-release-2026-05-22.txt").write_text(text)
        print(f"[A] wrote press-release-2026-05-22.txt ({len(text)} chars)", flush=True)


def task_b_thumbnails(cdp: CDP) -> None:
    """Page-context-fetch all 6 PDF thumbnails."""
    print(f"[B] pulling {len(THUMB_NAMES)} thumbnails via in-page fetch", flush=True)
    # Make sure we're on a war.gov tab so credentials/Akamai cookies apply
    cdp.call("Page.navigate", {"url": "https://www.war.gov/UFO/"})
    try:
        cdp.wait_event("Page.loadEventFired", timeout=30.0)
    except TimeoutError:
        pass
    time.sleep(2.0)
    for name in THUMB_NAMES:
        url = f"{THUMB_BASE}/{name}.jpg"
        status, content = fetch_binary_in_page(cdp, url)
        out_path = THUMB_DIR / f"{name}.jpg"
        if status == 200 and content:
            out_path.write_bytes(content)
            print(f"[B]   OK {name}.jpg {len(content)} bytes", flush=True)
        else:
            print(f"[B]   FAIL {name}.jpg status={status}", flush=True)


def task_c_fbi_vault_part_15(cdp: CDP) -> None:
    """Try to fetch FBI Vault UFO Part 15 of 16."""
    print(f"[C] navigating → FBI Vault Part 15 page", flush=True)
    cdp.call("Page.navigate", {"url": FBI_PART_PAGE})
    try:
        cdp.wait_event("Page.loadEventFired", timeout=30.0)
    except TimeoutError:
        pass
    time.sleep(3.0)

    # Try to find the PDF link on the page (Plone reading-room standard pattern)
    link_js = (
        "(() => {"
        "  const links = Array.from(document.querySelectorAll('a[href]')).map(a => a.href);"
        "  const pdfish = links.filter(h => /\\.pdf(\\?|$)|at_download\\/file/i.test(h));"
        "  return {title: document.title, total: links.length, pdfish: pdfish.slice(0, 10)};"
        "})()"
    )
    res = cdp.call("Runtime.evaluate", {"expression": link_js, "returnByValue": True})
    link_val = res.get("result", {}).get("value", {}) or {}
    print(f"[C] page info: {json.dumps(link_val)}", flush=True)

    pdf_url = None
    for h in link_val.get("pdfish", []):
        if "at_download/file" in h or h.lower().endswith(".pdf"):
            pdf_url = h
            break
    if not pdf_url:
        pdf_url = FBI_PART_PDF_GUESS
        print(f"[C] using guess URL → {pdf_url}", flush=True)

    print(f"[C] page-context fetch → {pdf_url}", flush=True)
    status, content = fetch_binary_in_page(cdp, pdf_url)
    if status == 200 and content:
        out_path = FBI_DIR / "UFO-Part-15-of-16.pdf"
        out_path.write_bytes(content)
        print(f"[C] OK {out_path.name} {len(content)/1e6:.1f} MB", flush=True)
    else:
        # Maybe the page itself IS the PDF (some Vault items)
        print(f"[C] direct fetch failed status={status}; trying alternate URLs", flush=True)
        # Save the page HTML for inspection
        html_status, html_text = fetch_text_in_page(cdp, FBI_PART_PAGE)
        (FBI_DIR / "part-15-page.html").write_text(html_text or "")
        print(f"[C] saved page HTML for inspection ({len(html_text)} chars)", flush=True)


def main() -> int:
    tab = open_tab("about:blank")
    target_id = tab["id"]
    ws_url = tab["webSocketDebuggerUrl"]
    cdp = CDP(ws_url)
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Network.enable", {"maxPostDataSize": 0})

        task_a_press_release(cdp)
        task_b_thumbnails(cdp)
        task_c_fbi_vault_part_15(cdp)

        return 0
    finally:
        cdp.close()
        close_tab(target_id)


if __name__ == "__main__":
    sys.exit(main())
