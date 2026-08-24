#!/usr/bin/env node

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function waitFor(check, message) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (await check()) return;
    await sleep(50);
  }
  throw new Error(message);
}

async function qualify() {
  const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const scopeHelpers = await import(pathToFileURL(path.join(repo, "src/skdashboard/static/js/control_plane_scope.js")));
  const directNow = Date.parse("2026-08-24T00:00:00Z");
  const directId = "sv-0123456789abcdef0123456789abcdef";
  const directContext = { role: "architect", scope: "estate", window: "latest", baseline: "none", service: "all", selected_silo: "flow", truth: "", saved_view: directId };
  const directFutureView = {
    schema_version: scopeHelpers.SCOPE_SCHEMA, id: directId, label: "flow",
    created_at: "2099-01-01T00:00:00.000Z", expires_at: "2099-01-02T00:00:00.000Z",
    route: "/control-plane/now",
    context: { role: "architect", scope: "estate", window: "latest", baseline: "none", service: "all" },
    filters: { selected_silo: "flow", truth: "" }, presentation: { workspace: "now" },
    registry_version: scopeHelpers.REGISTRY_VERSION, registry_hash: scopeHelpers.REGISTRY_HASH,
  };
  const directStorage = { getItem: () => JSON.stringify([directFutureView]) };
  assert.deepEqual(scopeHelpers.parseUrl(`?${scopeHelpers.safeSearch(directContext)}`, directStorage, directNow), { ok: false, state: "stale", message: "The saved view is stale." });
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-20-home-"));
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-20-cdp-"));
  const port = 17879;
  const python = `
import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import uvicorn
from skdashboard.control_plane_adapters import Reader, project_estate as real_project
from skdashboard.dashboard import create_app
repo = Path(${JSON.stringify(repo)})
fixture = json.loads((repo / "tests/fixtures/control_plane_full_estate.v1.0.0.json").read_text())
readers = {}
for case in fixture["estate_cases"]:
    if case.get("failure"):
        readers[case["adapter_id"]] = Reader(failure=case["failure"])
    else:
        readers[case["adapter_id"]] = Reader(payload={
            "schema_version": fixture["schema_version"],
            "observed_at": case.get("observed_at", fixture["observed_at"]),
            "watermark": case["watermark"],
            "coverage": case["coverage"],
            "aggregate": case["aggregate"],
            "errors": case["errors"],
            "has_observations": case["has_observations"],
        })
now = datetime.fromisoformat(fixture["projected_at"].replace("Z", "+00:00"))
patch("skdashboard.control_plane_adapters.default_readers", return_value=readers).start()
patch("skdashboard.control_plane_adapters.project_estate", side_effect=lambda value: real_project(value, now=now)).start()
def authorize(bearer, capability, target):
    return bearer == "now-cdp" and capability == "skdashboard.read"
app = create_app(Path(${JSON.stringify(home)}), control_plane_authorizer=authorize)
class ScopeBoundaryHarness:
    async def __call__(self, scope, receive, send):
        query = scope.get("query_string", b"").decode("ascii", "ignore")
        if scope.get("path") == "/api/v1/overview" and "selected_silo=portfolio" in query:
            await asyncio.sleep(0.35)
        if scope.get("path") != "/api/v1/overview" or "selected_silo=architecture" not in query:
            await app(scope, receive, send)
            return
        messages = []
        async def capture(message):
            messages.append(message)
        await app(scope, receive, capture)
        for message in messages:
            if message["type"] == "http.response.body" and message.get("body"):
                body = json.loads(message["body"])
                body["scope"]["selected_silo"] = "fleet"
                message["body"] = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
                for start in messages:
                    if start["type"] == "http.response.start":
                        start["headers"] = [(key, str(len(message["body"])).encode() if key.lower() == b"content-length" else value) for key, value in start["headers"]]
        for message in messages:
            await send(message)
uvicorn.run(ScopeBoundaryHarness(), host="127.0.0.1", port=${port}, log_level="error")
`;
  const server = spawn(process.env.PYTHON || "python", ["-c", python], {
    cwd: repo,
    env: { ...process.env, PYTHONPATH: path.join(repo, "src") },
    stdio: "ignore",
  });
  const chrome = spawn(
    process.env.CHROME_PATH || "/usr/bin/google-chrome",
    ["--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"],
    { stdio: "ignore" },
  );

  try {
    await waitFor(async () => fetch(`http://127.0.0.1:${port}/control-plane/now`).then((response) => response.ok).catch(() => false), "Dashboard did not start");
    const activePort = path.join(profile, "DevToolsActivePort");
    await waitFor(() => fs.existsSync(activePort), "Chrome did not publish DevToolsActivePort");
    const chromePort = fs.readFileSync(activePort, "utf8").trim().split("\n")[0];
    const targets = await fetch("http://127.0.0.1:" + chromePort + "/json/list").then((response) => response.json());
    const target = targets.find((candidate) => candidate.type === "page");
    const socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });

    let nextId = 0;
    const pending = new Map();
    const requests = [];
    const exceptions = [];
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) {
        const handlers = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) handlers.reject(new Error(JSON.stringify(message.error)));
        else handlers.resolve(message.result);
      }
      if (message.method === "Network.requestWillBeSent") requests.push(message.params.request);
      if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
    };
    const send = (method, params = {}) => new Promise((resolve, reject) => {
      nextId += 1;
      pending.set(nextId, { resolve, reject });
      socket.send(JSON.stringify({ id: nextId, method, params }));
    });
    const evaluate = async (expression) => {
      const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      assert.equal(result.exceptionDetails, undefined);
      return result.result.value;
    };
    const pressKey = async (key, code = key, modifiers = 0) => {
      const virtual = key.length === 1 ? key.toUpperCase().charCodeAt(0) : ({ Enter: 13, Escape: 27, Tab: 9, ArrowDown: 40, ArrowUp: 38 }[key] || 0);
      await send("Input.dispatchKeyEvent", { type: "keyDown", key, code, modifiers, windowsVirtualKeyCode: virtual, nativeVirtualKeyCode: virtual });
      await send("Input.dispatchKeyEvent", { type: "keyUp", key, code, modifiers, windowsVirtualKeyCode: virtual, nativeVirtualKeyCode: virtual });
    };

    await send("Page.enable");
    await send("Runtime.enable");
    await send("Network.enable");
    await send("Accessibility.enable");
    await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
    await send("Network.setExtraHTTPHeaders", { headers: { Authorization: "Bearer now-cdp", Origin: "http://10.0.0.139:7778" } });
    await send("Page.navigate", { url: `http://127.0.0.1:${port}/control-plane/now?role=architect&scope=estate&window=latest&baseline=none&service=all` });
    try {
      await waitFor(async () => evaluate("document.querySelectorAll('#estate-rows tr[data-silo]').length === 12").catch(() => false), "Estate pulse did not render");
    } catch (error) {
      error.message += `: ${await evaluate("JSON.stringify({estate:document.getElementById('estate-rows').innerText,count:document.getElementById('estate-count').innerText,quality:document.getElementById('quality-summary').innerText,url:location.href})")} ${JSON.stringify(exceptions)}`;
      throw error;
    }

    const desktop = JSON.parse(await evaluate(`JSON.stringify((() => ({
      url: location.pathname + location.search,
      rows: document.querySelectorAll('#estate-rows tr[data-silo]').length,
      sources: [...document.querySelectorAll('#estate-rows tr[data-silo]')].reduce((total, row) => total + Number(row.dataset.sourceCount), 0),
      evidenceButtons: document.querySelectorAll('.estate-evidence-button').length,
      metricVersions: [...document.querySelectorAll('#estate-rows td:nth-child(4)')].every((node) => node.textContent.includes('@1.0.0') && node.textContent.includes('scope estate') && node.textContent.includes('window latest')),
      baselineUnknown: [...document.querySelectorAll('#estate-rows td:nth-child(5)')].every((node) => node.textContent.includes('Unknown') && node.textContent.includes('No comparable baseline')),
      ai: document.querySelector('.ai-abstention').textContent,
      legal: document.querySelector('[data-silo=legal]').textContent,
      flowMetric: document.querySelector('[data-silo=flow] td:nth-child(4)').textContent,
      economyMetric: document.querySelector('[data-silo=economy] td:nth-child(4)').textContent,
      count: document.getElementById('estate-count').textContent,
    }))())`));
    assert.equal(desktop.url, "/control-plane/now?role=architect&scope=estate&window=latest&baseline=none&service=all");
    assert.equal(desktop.rows, 12);
    assert.equal(desktop.sources, 16);
    assert.equal(desktop.evidenceButtons, 12);
    assert.equal(desktop.metricVersions, true);
    assert.equal(desktop.baselineUnknown, true);
    assert.match(desktop.ai, /AI abstained/);
    assert.match(desktop.ai, /will not invent/);
    assert.match(desktop.legal, /Policy filtered/);
    assert.match(desktop.flowMetric, /skcoord\.flow \(task_flow\): 8 of 9/);
    assert.match(desktop.flowMetric, /skcoord\.agent_presence \(agent_presence\): 4 of 4/);
    assert.doesNotMatch(desktop.flowMetric, /12 of 13/);
    assert.match(desktop.economyMetric, /registry source skcounter\.harness/);
    assert.match(desktop.economyMetric, /source observation appears in another silo/);
    assert.doesNotMatch(desktop.economyMetric, /registry source skperf\.aggregate/);
    assert.equal(desktop.count, "12 silos | 16 sources");

    const contrast = JSON.parse(await evaluate(`JSON.stringify((() => {
      const parse = (value) => (value.match(/[\\d.]+/g) || []).map(Number);
      const luminance = (value) => {
        const [r, g, b] = parse(value).slice(0, 3).map((part) => part / 255).map((part) => part <= .04045 ? part / 12.92 : ((part + .055) / 1.055) ** 2.4);
        return .2126 * r + .7152 * g + .0722 * b;
      };
      const background = (node) => {
        for (let current = node; current; current = current.parentElement) {
          const value = getComputedStyle(current).backgroundColor;
          const parts = parse(value);
          if (parts.length === 3 || (parts.length > 3 && parts[3] > 0)) return value;
        }
        return 'rgb(255,255,255)';
      };
      const ratio = (node) => {
        const foreground = luminance(getComputedStyle(node).color);
        const behind = luminance(background(node));
        return (Math.max(foreground, behind) + .05) / (Math.min(foreground, behind) + .05);
      };
      const nodes = [...document.querySelectorAll('.now-head p,.now-kicker,.now-context label,.now-status-card p,.ai-fields dt,.estate-pulse thead th,.estate-pulse td small,.estate-evidence-button,#estate-rows .truth-badge')];
      return { minimum: Math.min(...nodes.map(ratio)), failures: nodes.filter((node) => ratio(node) < 4.5).map((node) => ({ text: node.textContent.trim(), ratio: ratio(node), color: getComputedStyle(node).color, background: background(node) })) };
    })())`));
    assert.equal(contrast.failures.length, 0, JSON.stringify(contrast.failures));
    assert.ok(contrast.minimum >= 4.5);
    const oldAccentRatio = await evaluate(`(() => {
      const style = document.createElement('style'); style.textContent = '.estate-evidence-button{color:#1f8fa8!important}'; document.head.append(style);
      const node = document.querySelector('.estate-evidence-button');
      const parts = (value) => (value.match(/[\\d.]+/g) || []).map(Number).slice(0,3).map((part) => part / 255).map((part) => part <= .04045 ? part / 12.92 : ((part + .055) / 1.055) ** 2.4);
      const lum = (value) => { const [r,g,b] = parts(value); return .2126*r+.7152*g+.0722*b; };
      const foreground = lum(getComputedStyle(node).color); const behind = lum('rgb(255,255,255)');
      style.remove(); return (Math.max(foreground,behind)+.05)/(Math.min(foreground,behind)+.05);
    })()`);
    assert.ok(oldAccentRatio < 4.5);
    const screenshotPath = path.join(os.tmpdir(), "skcp-20-unified-scope.png");
    const screenshot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    fs.writeFileSync(screenshotPath, screenshot.data, "base64");

    const tree = await send("Accessibility.getFullAXTree");
    const accessible = tree.nodes.map((node) => ({ role: node.role && node.role.value, name: node.name && node.name.value }));
    assert.ok(accessible.some((node) => node.role === "heading" && node.name === "Estate pulse"));
    assert.ok(accessible.some((node) => node.role === "button" && node.name === "Evidence for Portfolio and projects"));

    await evaluate("document.querySelector('.estate-evidence-button').focus()");
    await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", text: "\r", unmodifiedText: "\r", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
    assert.equal(await evaluate("document.getElementById('estate-evidence').open"), true);
    const evidenceText = await evaluate("document.getElementById('estate-evidence').textContent");
    assert.match(evidenceText, /portfolio.blocked_objectives@1.0.0/);
    assert.match(evidenceText, /synthetic-portfolio-r1/);
    assert.match(evidenceText, /does not refresh, remediate, queue, authorize, or dispatch/);
    await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
    assert.equal(await evaluate("document.getElementById('estate-evidence').open"), false);
    assert.equal(await evaluate("document.activeElement.classList.contains('estate-evidence-button')"), true);

    await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", text: "\r", unmodifiedText: "\r", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
    assert.equal(await evaluate("document.getElementById('estate-evidence').open"), true);
    assert.match(await evaluate("document.getElementById('estate-evidence-body').textContent"), /synthetic-portfolio-r1/);

    await evaluate("document.getElementById('now-selected-silo').value='flow'; document.getElementById('now-selected-silo').dispatchEvent(new Event('change',{bubbles:true}))");
    await waitFor(async () => evaluate("document.querySelectorAll('#estate-rows tr[data-silo]').length === 1").catch(() => false), "Silo cross-filter did not render");
    const flowFilter = JSON.parse(await evaluate(`JSON.stringify({
      url: location.pathname + location.search,
      silo: document.querySelector('#estate-rows tr[data-silo]').dataset.silo,
      sources: document.querySelector('#estate-rows tr[data-silo]').dataset.sourceCount,
      issueSources: [...document.querySelectorAll('.quality-issue dl div:first-child dd')].map((node) => node.textContent),
      context: [...document.querySelectorAll('[data-context-summary]')].map((node) => node.textContent),
      legacyHidden: document.getElementById('legacy-overview').hidden && document.getElementById('legacy-details').hidden,
      evidenceNames: [...document.querySelectorAll('.estate-evidence-button')].map((node) => node.getAttribute('aria-label')),
      share: document.getElementById('share-link').value,
    })`));
    assert.equal(flowFilter.silo, "flow");
    assert.equal(flowFilter.sources, "2");
    assert.ok(flowFilter.issueSources.every((value) => /skcoord\.flow|skcoord\.agent_presence/.test(value)));
    assert.ok(flowFilter.context.every((value) => value.includes("Agile flow")));
    assert.equal(flowFilter.legacyHidden, true);
    assert.deepEqual(flowFilter.evidenceNames, ["Evidence for Agile flow"]);
    assert.match(flowFilter.url, /selected_silo=flow/);
    assert.doesNotMatch(flowFilter.share, /saved_view=/);

    await evaluate("history.back()");
    await waitFor(async () => evaluate("document.querySelectorAll('#estate-rows tr[data-silo]').length === 12").catch(() => false), "History back did not restore the estate");
    await evaluate("history.forward()");
    await waitFor(async () => evaluate("document.querySelector('#estate-rows tr[data-silo]')?.dataset.silo === 'flow'").catch(() => false), "History forward did not restore the filter");

    await evaluate(`(() => {
      const select = document.getElementById('now-selected-silo');
      select.value='portfolio'; select.dispatchEvent(new Event('change',{bubbles:true}));
      select.value='flow'; select.dispatchEvent(new Event('change',{bubbles:true}));
    })()`);
    await waitFor(async () => evaluate("document.querySelector('#estate-rows tr[data-silo]')?.dataset.silo === 'flow'").catch(() => false), "Latest scope did not win the race");
    await sleep(500);
    assert.equal(await evaluate("document.querySelector('#estate-rows tr[data-silo]')?.dataset.silo"), "flow");

    await evaluate("document.getElementById('save-view').click()");
    await waitFor(async () => evaluate("new URLSearchParams(location.search).has('saved_view')").catch(() => false), "Saved view was not activated");
    const saved = JSON.parse(await evaluate(`JSON.stringify((() => {
      const records = JSON.parse(localStorage.getItem('skdashboard.control-plane.saved-views.v1'));
      return { url: location.href, record: records[0], status: document.getElementById('saved-view-status').textContent };
    })())`));
    assert.match(saved.record.id, /^sv-[0-9a-f]{32}$/);
    assert.deepEqual(Object.keys(saved.record).sort(), ["context", "created_at", "expires_at", "filters", "id", "label", "presentation", "registry_hash", "registry_version", "route", "schema_version"].sort());
    assert.equal(JSON.stringify(saved.record).match(/token|capability|authorization|tenant|matter|watermark|evidence|prompt/gi), null);
    assert.match(saved.status, /Active saved view|Saved/);
    const savedUrl = saved.url;
    const safeShareUrl = await evaluate("document.getElementById('share-link').value");
    assert.doesNotMatch(safeShareUrl, /saved_view=/);

    await send("Page.reload");
    await waitFor(async () => evaluate("document.querySelector('#estate-rows tr[data-silo]')?.dataset.silo === 'flow'").catch(() => false), "Saved view did not survive refresh");
    assert.match(await evaluate("document.getElementById('saved-view-status').textContent"), /Active saved view/);
    await evaluate("document.querySelector('.estate-evidence-button').click()");
    assert.match(await evaluate("document.getElementById('estate-evidence-body').textContent"), /synthetic-flow-r1/);
    await pressKey("Escape", "Escape");
    await pressKey("k", "KeyK", 2);
    assert.equal(await evaluate("document.getElementById('command-palette').open"), true);
    await send("Network.setExtraHTTPHeaders", { headers: { Origin: "http://10.0.0.139:7778" } });
    await evaluate("window.dispatchEvent(new PopStateEvent('popstate'))");
    await waitFor(async () => evaluate("document.getElementById('estate-count').textContent === 'Unavailable'").catch(() => false), "401 revocation did not fail closed");
    assert.equal(await evaluate("document.getElementById('command-palette').open"), false);
    assert.equal(await evaluate("document.getElementById('estate-evidence').open"), false);
    assert.equal(await evaluate("document.getElementById('estate-evidence-body').textContent"), "");
    assert.equal(await evaluate("document.getElementById('quality-preview-body').textContent"), "");
    assert.equal(await evaluate("document.querySelectorAll('.chip.ok').length"), 0);
    assert.equal(await evaluate("document.body.textContent.includes('synthetic-flow-r1')"), false);
    assert.match(await evaluate("document.getElementById('saved-view-status').textContent"), /Unauthorized or revoked/);
    await send("Network.setExtraHTTPHeaders", { headers: { Authorization: "Bearer now-cdp", Origin: "http://10.0.0.139:7778" } });
    await send("Page.navigate", { url: savedUrl });
    await waitFor(async () => evaluate("document.querySelector('#estate-rows tr[data-silo]')?.dataset.silo === 'flow'").catch(() => false), "Saved view did not recover after 401 test");
    await send("Network.setExtraHTTPHeaders", { headers: { Authorization: "Bearer denied-cdp", Origin: "http://10.0.0.139:7778" } });
    await evaluate("window.dispatchEvent(new PopStateEvent('popstate'))");
    await waitFor(async () => evaluate("document.getElementById('estate-count').textContent === 'Unavailable'").catch(() => false), "403 revocation did not fail closed");
    assert.match(await evaluate("document.getElementById('saved-view-status').textContent"), /Unauthorized or revoked/);
    await send("Network.setExtraHTTPHeaders", { headers: { Authorization: "Bearer now-cdp", Origin: "http://10.0.0.139:7778" } });
    await send("Page.navigate", { url: safeShareUrl });
    await waitFor(async () => evaluate("document.querySelector('#estate-rows tr[data-silo]')?.dataset.silo === 'flow'").catch(() => false), "Safe share link did not restore context");
    assert.equal(await evaluate("new URLSearchParams(location.search).has('saved_view')"), false);

    await send("Page.navigate", { url: savedUrl });
    await waitFor(async () => evaluate("document.querySelector('#estate-rows tr[data-silo]')?.dataset.silo === 'flow'").catch(() => false), "Saved view did not reopen");
    await evaluate(`(() => {
      const key='skdashboard.control-plane.saved-views.v1'; const records=JSON.parse(localStorage.getItem(key));
      const expires=Date.now()-1000; records[0].expires_at=new Date(expires).toISOString(); records[0].created_at=new Date(expires-86400000).toISOString();
      localStorage.setItem(key,JSON.stringify(records));
    })()`);
    const expiryRequestMark = requests.length;
    await send("Page.navigate", { url: savedUrl });
    await waitFor(async () => evaluate("document.getElementById('saved-view-status').textContent.includes('expired')").catch(() => false), "Expired view did not fail closed");
    assert.equal(requests.slice(expiryRequestMark).filter((request) => request.url.includes("/api/v1/overview")).length, 0);

    await send("Page.navigate", { url: safeShareUrl });
    await waitFor(async () => evaluate("document.querySelector('#estate-rows tr[data-silo]')?.dataset.silo === 'flow'").catch(() => false), "Safe context did not recover after expiry");
    await evaluate("document.getElementById('save-view').click()");
    await waitFor(async () => evaluate("new URLSearchParams(location.search).has('saved_view')").catch(() => false), "Replacement view did not save");
    const tamperedUrl = await evaluate("location.href");
    await evaluate(`(() => { const key='skdashboard.control-plane.saved-views.v1'; const records=JSON.parse(localStorage.getItem(key)); records[0].schema_version='tampered'; localStorage.setItem(key,JSON.stringify(records)); })()`);
    const tamperRequestMark = requests.length;
    await send("Page.navigate", { url: tamperedUrl });
    await waitFor(async () => evaluate("document.getElementById('saved-view-status').textContent.includes('stale')").catch(() => false), "Tampered view did not fail closed");
    assert.equal(requests.slice(tamperRequestMark).filter((request) => request.url.includes("/api/v1/overview")).length, 0);

    await send("Page.navigate", { url: safeShareUrl });
    await waitFor(async () => evaluate("document.querySelector('#estate-rows tr[data-silo]')?.dataset.silo === 'flow'").catch(() => false), "Future-tamper lane did not restore");
    await evaluate("document.getElementById('save-view').click()");
    await waitFor(async () => evaluate("new URLSearchParams(location.search).has('saved_view')").catch(() => false), "Future-tamper view did not save");
    const futureUrl = await evaluate("location.href");
    await evaluate(`(() => {
      const key='skdashboard.control-plane.saved-views.v1'; const records=JSON.parse(localStorage.getItem(key));
      records[0].created_at='2099-01-01T00:00:00.000Z'; records[0].expires_at='2099-01-02T00:00:00.000Z';
      localStorage.setItem(key,JSON.stringify(records));
    })()`);
    const futureRequestMark = requests.length;
    await send("Page.navigate", { url: futureUrl });
    await waitFor(async () => evaluate("document.getElementById('saved-view-status').textContent.includes('stale')").catch(() => false), "Future-issued view did not fail closed");
    assert.equal(requests.slice(futureRequestMark).filter((request) => request.url.includes("/api/v1/overview")).length, 0);
    assert.equal(await evaluate("document.querySelectorAll('#estate-rows tr[data-silo]').length"), 0);

    await send("Page.navigate", { url: safeShareUrl });
    await waitFor(async () => evaluate("document.querySelector('#estate-rows tr[data-silo]')?.dataset.silo === 'flow'").catch(() => false), "Palette lane did not restore");
    await pressKey("k", "KeyK", 2);
    await waitFor(async () => evaluate("document.getElementById('command-palette').open").catch(() => false), "Ctrl K did not open command search");
    const paletteTree = await send("Accessibility.getFullAXTree");
    const paletteAX = paletteTree.nodes.map((node) => ({ role: node.role && node.role.value, name: node.name && node.name.value }));
    assert.ok(paletteAX.some((node) => node.role === "dialog" && /Search this authorized view/.test(node.name)));
    for (const role of ["combobox", "listbox", "option"]) assert.ok(paletteAX.some((node) => node.role === role));
    for (let index = 0; index < 12; index += 1) await pressKey("Tab", "Tab");
    assert.equal(await evaluate("document.getElementById('command-palette').contains(document.activeElement)"), true);
    await pressKey("Escape", "Escape");
    assert.equal(await evaluate("document.getElementById('command-palette').open"), false);
    await waitFor(async () => evaluate("document.activeElement.id === 'command-trigger'").catch(() => false), "Command focus did not return to its trigger");

    await pressKey("k", "KeyK", 4);
    await evaluate("document.getElementById('command-search').value='Reports'; document.getElementById('command-search').dispatchEvent(new Event('input',{bubbles:true}))");
    await pressKey("Enter", "Enter");
    assert.match(await evaluate("document.getElementById('command-status').textContent"), /Reports unavailable/);
    await evaluate("document.getElementById('command-search').value='Agile flow evidence'; document.getElementById('command-search').dispatchEvent(new Event('input',{bubbles:true}))");
    await pressKey("ArrowDown", "ArrowDown");
    await pressKey("ArrowUp", "ArrowUp");
    await pressKey("Enter", "Enter");
    assert.equal(await evaluate("document.getElementById('estate-evidence').open"), true);
    assert.match(await evaluate("document.getElementById('estate-evidence-title').textContent"), /Agile flow evidence/);
    await pressKey("Escape", "Escape");

    await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
    assert.equal(await evaluate("(() => { const node = document.createElement('div'); node.className = 'spinner'; document.body.append(node); const name = getComputedStyle(node).animationName; node.remove(); return name; })()"), "none");
    for (const width of [390, 320]) {
      await send("Emulation.setDeviceMetricsOverride", { width, height: 844, deviceScaleFactor: 1, mobile: true });
      assert.equal(await evaluate("document.documentElement.scrollWidth <= innerWidth"), true);
    }

    await send("Page.navigate", { url: `http://127.0.0.1:${port}/control-plane/now?role=architect&scope=estate&window=latest&baseline=none&service=all&selected_silo=architecture` });
    await waitFor(async () => evaluate("document.getElementById('estate-count').textContent === 'Unavailable'").catch(() => false), "Mismatched response scope did not fail closed");
    assert.match(await evaluate("document.getElementById('estate-rows').textContent"), /Response scope did not match/);
    assert.equal(await evaluate("document.body.textContent.includes('synthetic-cmdb-r1')"), false);

    const protectedValue = "never-retain-this-value";
    for (const query of [`tenant_id=${protectedValue}`, `matter_id=${protectedValue}`, `token=${protectedValue}`, "scope=estate&scope=estate", `selected_silo=${"x".repeat(129)}`]) {
      const mark = requests.length;
      await send("Page.navigate", { url: `http://127.0.0.1:${port}/control-plane/now?${query}` });
      await waitFor(async () => evaluate("location.search.includes('role=operator') && document.getElementById('estate-count').textContent === 'Unavailable'").catch(() => false), "Unsafe deep link did not fail closed");
      assert.equal(requests.slice(mark).filter((request) => request.url.includes("/api/v1/overview")).length, 0);
      assert.equal(await evaluate(`document.body.textContent.includes(${JSON.stringify(protectedValue)}) || (localStorage.getItem('skdashboard.control-plane.saved-views.v1') || '').includes(${JSON.stringify(protectedValue)})`), false);
    }

    const external = requests.filter((request) => /^https?:/.test(request.url) && !request.url.startsWith(`http://127.0.0.1:${port}/`));
    assert.deepEqual(requests.filter((request) => request.method !== "GET"), []);
    assert.deepEqual(external, []);
    assert.equal(exceptions.length, 0);
    const userAgent = await evaluate("navigator.userAgent");
    socket.close();
    return { result: "PASS", userAgent, rows: 12, sources: 16, perPopulationCoverage: "PASS", registryProvenance: "PASS", scopeResponseBinding: "PASS", crossFiltering: "PASS", latestScopeRace: "PASS", historyRefreshShare: "PASS", savedViewLifecycle: "PASS", unsafeDeepLinksPreRequestBlock: "PASS", commandPaletteKeyboardAX: "PASS", keyboardEvidence: "PASS", liveAuthRevocationPurge: "PASS", authFailClosed: "PASS", minimumContrast: contrast.minimum, contrastSensitivity: "PASS", responsiveWidths: [390, 320], reducedMotion: "PASS", nonGetRequests: 0, externalRequests: 0, runtimeExceptions: 0, screenshotPath };
  } finally {
    chrome.kill("SIGTERM");
    server.kill("SIGTERM");
    await sleep(100);
    chrome.kill("SIGKILL");
    server.kill("SIGKILL");
    await sleep(50);
    fs.rmSync(profile, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
}

qualify().then((result) => console.log(JSON.stringify(result, null, 2))).catch((error) => {
  console.error(error.stack);
  process.exitCode = 1;
});
