#!/usr/bin/env python3
"""Discover + capture the DOJ SPLC press release by NAVIGATING the search page
(so JS renders the result list) then reading the rendered DOM. Falls back to
scraping any /opa/pr/ or /news/ links the rendered page exposes.
"""
from __future__ import annotations
import json, re, sys, time, urllib.request
from pathlib import Path
import websocket

CDP_HTTP = "http://127.0.0.1:9222"
SEARCH_URL = "https://www.justice.gov/news?search_api_fulltext=Southern%20Poverty%20Law%20Center"
OUT = Path("/home/cbrd21/clawd/skills/substance-lens/captures/splc-doj-2026-06-03")


def open_tab(url):
    req = urllib.request.Request(f"{CDP_HTTP}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def close_tab(tid):
    try:
        urllib.request.urlopen(f"{CDP_HTTP}/json/close/{tid}", timeout=5)
    except Exception:
        pass

class CDP:
    def __init__(self, ws): self.ws=websocket.create_connection(ws,timeout=120); self.mid=0
    def call(self, m, p=None, t=60.0):
        self.mid+=1; i=self.mid
        self.ws.send(json.dumps({"id":i,"method":m,"params":p or {}})); self.ws.settimeout(t)
        while True:
            msg=json.loads(self.ws.recv())
            if msg.get("id")==i:
                if "error" in msg: raise RuntimeError(f"{m}: {msg['error']}")
                return msg.get("result",{})
    def wait(self, name, t=30.0):
        end=time.time()+t
        while time.time()<end:
            self.ws.settimeout(max(0.1,end-time.time()))
            try: msg=json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException: continue
            if msg.get("method")==name: return msg.get("params",{})
        return {}
    def close(self):
        try: self.ws.close()
        except Exception: pass

def jseval(cdp, expr, t=60):
    r=cdp.call("Runtime.evaluate",{"expression":expr,"awaitPromise":True,"returnByValue":True},t)
    return r.get("result",{}).get("value")

def fetch_text(cdp,url):
    expr=(f"(async()=>{{try{{const r=await fetch({json.dumps(url)},{{credentials:'include',cache:'no-store'}});"
          f"return {{status:r.status,text:await r.text()}};}}catch(e){{return{{status:-1,text:String(e)}};}}}})()")
    v=jseval(cdp,expr,180) or {}
    return v.get("status",0), v.get("text","")

def main():
    tab=open_tab(SEARCH_URL); tid=tab["id"]; cdp=CDP(tab["webSocketDebuggerUrl"])
    try:
        cdp.call("Page.enable"); cdp.call("Runtime.enable")
        cdp.call("Page.navigate",{"url":SEARCH_URL})
        cdp.wait("Page.loadEventFired",30.0)
        time.sleep(6.0)  # let result JS render
        links=jseval(cdp,
            "JSON.stringify(Array.from(document.querySelectorAll('a[href]'))"
            ".map(a=>({h:a.getAttribute('href'),t:(a.innerText||'').trim()}))"
            ".filter(x=>x.h&&(x.h.includes('/opa/pr/')||x.h.includes('/usao-mdal/pr/')||/southern.poverty|law.center|splc/i.test(x.t))))")
        cands=json.loads(links) if links else []
        print(f"[pr] rendered candidates: {len(cands)}", flush=True)
        for c in cands[:15]: print("   ", c["h"], "::", c["t"][:70], flush=True)
        # pick best
        pr=None
        for c in cands:
            if re.search(r"southern.poverty|law.center|splc|wire.fraud", (c["h"]+c["t"]).lower()):
                pr=c["h"]; break
        if not pr and cands: pr=cands[0]["h"]
        if pr and pr.startswith("/"): pr="https://www.justice.gov"+pr
        manifest={"search_url":SEARCH_URL,"rendered_candidates":cands[:15],"chosen":pr}
        if pr:
            print(f"[pr] fetching -> {pr}", flush=True)
            st,html=fetch_text(cdp,pr)
            print(f"[pr] status={st} len={len(html)}", flush=True)
            if st==200 and html:
                (OUT/"doj-press-release.html").write_text(html)
                txt=jseval(cdp,
                    f"(async()=>{{const r=await fetch({json.dumps(pr)},{{credentials:'include'}});"
                    f"const h=await r.text();const d=new DOMParser().parseFromString(h,'text/html');"
                    f"const a=d.querySelector('.field--name-body')||d.querySelector('article')||d.querySelector('main')||d.body;"
                    f"return a?a.innerText:'';}})()",60) or ""
                if txt: (OUT/"doj-press-release.txt").write_text(txt); print(f"[pr] {len(txt)} chars text", flush=True)
                manifest["status"]=st; manifest["txt_chars"]=len(txt)
        (OUT/"press-release-discovery.json").write_text(json.dumps(manifest,indent=2))
        print("[pr] done", flush=True)
        return 0
    finally:
        cdp.close(); close_tab(tid)

if __name__=="__main__":
    sys.exit(main())
