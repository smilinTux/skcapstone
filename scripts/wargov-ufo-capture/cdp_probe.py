#!/usr/bin/env python3
"""Probe war.gov/UFO/ via Lumina Chrome CDP (port 9222).

Steps:
  1. Open a new tab on war.gov/UFO/
  2. Wait for Vue mount to load (CSV must be reachable)
  3. Pull the CSV via in-page fetch
  4. Inspect inline scripts for any release_2 link patterns
  5. Save raw CSV + script index to ~/clawd/tmp/wargov-capture/probe-out/

Output:
  probe-out/uap-csv.csv          fresh CSV from the site
  probe-out/file-index.json      inline-script link probe
  probe-out/page-meta.json       URL/title/page render check
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import websocket  # websocket-client

CDP_HTTP = "http://127.0.0.1:9222"
TARGET = "https://www.war.gov/UFO/"
OUT_DIR = Path("/home/cbrd21/clawd/tmp/wargov-capture/probe-out")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def cdp_get(path: str) -> dict | list:
    with urllib.request.urlopen(f"{CDP_HTTP}{path}") as r:
        return json.loads(r.read())


def open_tab(url: str) -> dict:
    # Newer Chrome only accepts PUT on /json/new
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
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self.mid = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
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


def main() -> int:
    print(f"[probe] opening tab → {TARGET}", flush=True)
    tab = open_tab(TARGET)
    target_id = tab["id"]
    ws_url = tab["webSocketDebuggerUrl"]
    print(f"[probe] tab id={target_id}", flush=True)

    cdp = CDP(ws_url)
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Network.enable", {"maxPostDataSize": 0})
        cdp.call("Page.navigate", {"url": TARGET})
        try:
            cdp.wait_event("Page.loadEventFired", timeout=30.0)
        except TimeoutError:
            print("[probe] Page.loadEventFired timeout — proceeding anyway", flush=True)

        # Give the Vue mount a chance to render the CSV view
        time.sleep(5.0)

        # Page meta
        meta_js = (
            "({"
            "  url: location.href,"
            "  title: document.title,"
            "  hasMainContent: !!document.querySelector('main'),"
            "  scriptInlineCount: document.querySelectorAll('script:not([src])').length,"
            "  ufoMentions: (document.body.innerText.match(/UAP|UFO|PURSUE/g) || []).length,"
            "  releaseDateGuesses: Array.from(new Set((document.body.innerText.match(/\\b\\d{1,2}\\/\\d{1,2}\\/\\d{2,4}\\b/g) || []))),"
            "  release2HrefCount: document.querySelectorAll('a[href*=\"release_2\"]').length,"
            "  release2InHtml: (document.documentElement.outerHTML.match(/release_2/gi) || []).length"
            "})"
        )
        meta = cdp.call("Runtime.evaluate", {"expression": meta_js, "returnByValue": True})
        meta_val = meta.get("result", {}).get("value", {})
        (OUT_DIR / "page-meta.json").write_text(json.dumps(meta_val, indent=2))
        print(f"[probe] page-meta: {json.dumps(meta_val)}", flush=True)

        # Pull the CSV via in-page fetch
        csv_js = (
            "(async () => {"
            "  const u = '/Portals/1/Interactive/2026/UFO/uap-csv.csv';"
            "  const r = await fetch(u, {credentials: 'include', cache: 'no-store'});"
            "  return {status: r.status, len: (await r.clone().text()).length, text: await r.text()};"
            "})()"
        )
        csv_res = cdp.call("Runtime.evaluate", {
            "expression": csv_js,
            "awaitPromise": True,
            "returnByValue": True,
        }, timeout=60)
        csv_val = csv_res.get("result", {}).get("value", {})
        if isinstance(csv_val, dict) and csv_val.get("status") == 200:
            (OUT_DIR / "uap-csv.csv").write_text(csv_val["text"])
            print(f"[probe] CSV pulled, {csv_val['len']} bytes", flush=True)
        else:
            print(f"[probe] CSV fetch failed: {csv_val}", flush=True)
            (OUT_DIR / "uap-csv-error.json").write_text(json.dumps(csv_val, indent=2, default=str))

        # Inspect inline scripts for release_2 hints
        scripts_js = (
            "(() => {"
            "  const out = [];"
            "  document.querySelectorAll('script:not([src])').forEach((s, i) => {"
            "    const t = s.textContent || '';"
            "    out.push({idx: i, len: t.length, hasRelease2: /release_2/i.test(t), hasFetch: /fetch\\(/.test(t), hasCsv: /\\.csv/.test(t), preview: t.slice(0, 400)});"
            "  });"
            "  return out;"
            "})()"
        )
        scripts_res = cdp.call("Runtime.evaluate", {"expression": scripts_js, "returnByValue": True})
        scripts_val = scripts_res.get("result", {}).get("value", [])
        (OUT_DIR / "inline-scripts.json").write_text(json.dumps(scripts_val, indent=2))
        print(f"[probe] inline scripts: {len(scripts_val)} ({sum(1 for s in scripts_val if s.get('hasRelease2'))} mention release_2)", flush=True)

        # Probe for press release link
        pr_js = (
            "(() => {"
            "  const links = Array.from(document.querySelectorAll('a[href]')).map(a => a.href);"
            "  const press = links.filter(h => /News\\/Releases/i.test(h));"
            "  const medialink = links.filter(h => /medialink\\/ufo/i.test(h));"
            "  return {pressCount: press.length, press: press.slice(0, 20), medialinkCount: medialink.length, medialinkSample: medialink.slice(0, 20)};"
            "})()"
        )
        pr_res = cdp.call("Runtime.evaluate", {"expression": pr_js, "returnByValue": True})
        pr_val = pr_res.get("result", {}).get("value", {})
        (OUT_DIR / "link-probe.json").write_text(json.dumps(pr_val, indent=2))
        print(f"[probe] link probe: press={pr_val.get('pressCount')} medialink={pr_val.get('medialinkCount')}", flush=True)

        print("[probe] DONE", flush=True)
        return 0
    finally:
        cdp.close()
        close_tab(target_id)


if __name__ == "__main__":
    sys.exit(main())
