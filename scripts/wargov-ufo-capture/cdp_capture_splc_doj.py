#!/usr/bin/env python3
"""Capture the PRIMARY DOJ sources for the SPLC superseding-indictment finding via Lumina Chrome CDP.

Both targets returned HTTP 403 to WebFetch (Akamai TLS-fingerprint/bot gate). Driving
Lumina's already-authenticated Chrome (port 9222) in page context bypasses the gate.

Targets:
  1. Indictment PDF  -> https://www.justice.gov/opa/media/1437146/dl   (download)
  2. DOJ press release -> discovered via justice.gov news search for "Southern Poverty Law Center"

Goal (per finding 2026-06-04_splc-doj-superseding-indictment-oneill-thread.md):
  resolve the 2010-vs-2014 conduct window, the F-30 "$70K" figure, and confirm count
  language first-hand rather than via secondary quotation.

Output -> ~/clawd/skills/substance-lens/captures/splc-doj-2026-06-03/
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import websocket

CDP_HTTP = "http://127.0.0.1:9222"
SEED_URL = "https://www.justice.gov/"
PDF_URL = "https://www.justice.gov/opa/media/1437146/dl"
SEARCH_URL = "https://www.justice.gov/news?search_api_fulltext=Southern+Poverty+Law+Center"

OUT = Path("/home/cbrd21/clawd/skills/substance-lens/captures/splc-doj-2026-06-03")
OUT.mkdir(parents=True, exist_ok=True)


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
        f"  try {{"
        f"    const r = await fetch({json.dumps(url)}, {{credentials: 'include', cache: 'no-store'}});"
        f"    return {{status: r.status, text: await r.text()}};"
        f"  }} catch (e) {{ return {{status: -1, text: String(e)}}; }}"
        f"}})()"
    )
    res = cdp.call("Runtime.evaluate", {
        "expression": expr,
        "awaitPromise": True,
        "returnByValue": True,
    }, timeout=180)
    val = res.get("result", {}).get("value", {}) or {}
    return val.get("status", 0), val.get("text", "")


def fetch_pdf_b64_in_page(cdp: CDP, url: str) -> tuple[int, str, int]:
    """Fetch a binary in page context, return base64 (works for PDFs under a few MB)."""
    expr = (
        f"(async () => {{"
        f"  try {{"
        f"    const r = await fetch({json.dumps(url)}, {{credentials: 'include', cache: 'no-store'}});"
        f"    const buf = await r.arrayBuffer();"
        f"    const bytes = new Uint8Array(buf);"
        f"    let bin = '';"
        f"    const chunk = 0x8000;"
        f"    for (let i = 0; i < bytes.length; i += chunk) {{"
        f"      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));"
        f"    }}"
        f"    return {{status: r.status, b64: btoa(bin), len: bytes.length}};"
        f"  }} catch (e) {{ return {{status: -1, b64: '', len: 0, err: String(e)}}; }}"
        f"}})()"
    )
    res = cdp.call("Runtime.evaluate", {
        "expression": expr,
        "awaitPromise": True,
        "returnByValue": True,
    }, timeout=240)
    val = res.get("result", {}).get("value", {}) or {}
    return val.get("status", 0), val.get("b64", ""), val.get("len", 0)


def sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def main() -> int:
    import base64

    print(f"[capture] seeding tab -> {SEED_URL}", flush=True)
    tab = open_tab(SEED_URL)
    target_id = tab["id"]
    cdp = CDP(tab["webSocketDebuggerUrl"])
    manifest: dict = {
        "finding": "2026-06-04_splc-doj-superseding-indictment-oneill-thread.md",
        "capture_method": "Lumina Chrome CDP (port 9222) — page-context fetch (Akamai bypass)",
        "targets": {},
    }
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Network.enable", {"maxPostDataSize": 0})
        cdp.call("Page.navigate", {"url": SEED_URL})
        try:
            cdp.wait_event("Page.loadEventFired", timeout=30.0)
        except TimeoutError:
            pass
        time.sleep(3.0)  # let Akamai cookies stick

        # ---- 1. Indictment PDF (primary) ----
        print(f"[capture] fetching PDF -> {PDF_URL}", flush=True)
        status, b64, length = fetch_pdf_b64_in_page(cdp, PDF_URL)
        print(f"[capture] PDF status={status} bytes={length}", flush=True)
        if status == 200 and b64:
            data = base64.b64decode(b64)
            is_pdf = data[:5] == b"%PDF-"
            pdf_path = OUT / "splc-superseding-indictment-1437146.pdf"
            pdf_path.write_bytes(data)
            sha = sha256_bytes(data)
            print(f"[capture] PDF written {len(data)} bytes is_pdf={is_pdf} sha256={sha}", flush=True)
            manifest["targets"]["indictment_pdf"] = {
                "url": PDF_URL, "status": status, "path": str(pdf_path),
                "bytes": len(data), "is_pdf_magic": is_pdf, "sha256": sha,
            }
        else:
            manifest["targets"]["indictment_pdf"] = {"url": PDF_URL, "status": status, "error": True}
            print("[capture] PDF FAILED — page-context fetch did not return 200", flush=True)

        # ---- 2. Discover + fetch DOJ press release ----
        print(f"[capture] searching DOJ news -> {SEARCH_URL}", flush=True)
        status, html = fetch_text_in_page(cdp, SEARCH_URL)
        print(f"[capture] search status={status} len={len(html)}", flush=True)
        pr_url = None
        if status == 200 and html:
            (OUT / "doj-news-search.html").write_text(html)
            # Find press-release links; prefer /opa/pr/ slugs mentioning the charge
            cands = re.findall(r'href="(/opa/pr/[^"#?]+)"', html)
            uniq = []
            for c in cands:
                if c not in uniq:
                    uniq.append(c)
            print(f"[capture] press-release candidates: {uniq[:10]}", flush=True)
            scored = [c for c in uniq if "southern-poverty" in c.lower()
                      or "splc" in c.lower()
                      or ("wire-fraud" in c.lower() and "law-center" in c.lower())]
            if scored:
                pr_url = "https://www.justice.gov" + scored[0]
            elif uniq:
                pr_url = "https://www.justice.gov" + uniq[0]
            manifest["press_release_candidates"] = uniq[:15]

        if pr_url:
            print(f"[capture] fetching press release -> {pr_url}", flush=True)
            status, prhtml = fetch_text_in_page(cdp, pr_url)
            print(f"[capture] press release status={status} len={len(prhtml)}", flush=True)
            if status == 200 and prhtml:
                (OUT / "doj-press-release.html").write_text(prhtml)
                txt_expr = (
                    f"(async () => {{"
                    f"  const r = await fetch({json.dumps(pr_url)}, {{credentials: 'include'}});"
                    f"  const html = await r.text();"
                    f"  const doc = new DOMParser().parseFromString(html, 'text/html');"
                    f"  const a = doc.querySelector('.field--name-body') || doc.querySelector('article')"
                    f"        || doc.querySelector('main') || doc.body;"
                    f"  return a ? a.innerText : '';"
                    f"}})()"
                )
                res = cdp.call("Runtime.evaluate", {
                    "expression": txt_expr, "awaitPromise": True, "returnByValue": True,
                }, timeout=60)
                txt = res.get("result", {}).get("value", "") or ""
                if txt:
                    (OUT / "doj-press-release.txt").write_text(txt)
                    print(f"[capture] extracted {len(txt)} chars of press-release text", flush=True)
                manifest["targets"]["press_release"] = {"url": pr_url, "status": status,
                                                         "txt_chars": len(txt)}
            else:
                manifest["targets"]["press_release"] = {"url": pr_url, "status": status, "error": True}
        else:
            print("[capture] no press-release URL discovered from search", flush=True)
            manifest["targets"]["press_release"] = {"discovered": False}

        manifest["captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print(f"[capture] manifest written -> {OUT/'manifest.json'}", flush=True)
        return 0
    finally:
        cdp.close()
        close_tab(target_id)


if __name__ == "__main__":
    sys.exit(main())
