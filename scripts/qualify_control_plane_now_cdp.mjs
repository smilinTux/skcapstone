#!/usr/bin/env node

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

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
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-13-home-"));
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-13-cdp-"));
  const port = 17879;
  const python = `
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
uvicorn.run(create_app(Path(${JSON.stringify(home)}), control_plane_authorizer=authorize), host="127.0.0.1", port=${port}, log_level="error")
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

    await send("Page.enable");
    await send("Runtime.enable");
    await send("Network.enable");
    await send("Accessibility.enable");
    await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
    await send("Network.setExtraHTTPHeaders", { headers: { Authorization: "Bearer now-cdp", Origin: "http://10.0.0.139:7778" } });
    await send("Page.navigate", { url: `http://127.0.0.1:${port}/control-plane/now?role=architect&scope=wrong&window=30d&baseline=previous&service=sklegal` });
    await waitFor(async () => evaluate("document.querySelectorAll('#estate-rows tr[data-silo]').length === 12").catch(() => false), "Estate pulse did not render");

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
    const screenshotPath = path.join(os.tmpdir(), "skcp-13-now-workspace.png");
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

    await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
    assert.equal(await evaluate("(() => { const node = document.createElement('div'); node.className = 'spinner'; document.body.append(node); const name = getComputedStyle(node).animationName; node.remove(); return name; })()"), "none");
    for (const width of [390, 320]) {
      await send("Emulation.setDeviceMetricsOverride", { width, height: 844, deviceScaleFactor: 1, mobile: true });
      assert.equal(await evaluate("document.documentElement.scrollWidth <= innerWidth"), true);
    }

    await send("Network.setExtraHTTPHeaders", { headers: { Origin: "http://10.0.0.139:7778" } });
    await evaluate("document.getElementById('now-role').value='operator'; document.getElementById('now-role').dispatchEvent(new Event('change',{bubbles:true}))");
    await waitFor(async () => evaluate("document.getElementById('estate-count').textContent === 'Unavailable'").catch(() => false), "Unauthorized state did not fail closed");
    assert.match(await evaluate("document.getElementById('estate-rows').textContent"), /No silo is assumed healthy/);
    assert.equal(await evaluate("document.getElementById('estate-evidence').open"), false);
    assert.equal(await evaluate("document.getElementById('estate-evidence-title').textContent"), "Estate evidence unavailable");
    assert.equal(await evaluate("document.getElementById('estate-evidence-body').textContent"), "");
    assert.equal(await evaluate("document.getElementById('quality-preview-body').textContent"), "");
    assert.equal(await evaluate("document.querySelectorAll('.chip.ok').length"), 0);
    assert.equal(await evaluate("document.body.textContent.includes('synthetic-portfolio-r1')"), false);

    const external = requests.filter((request) => /^https?:/.test(request.url) && !request.url.startsWith(`http://127.0.0.1:${port}/`));
    assert.deepEqual(requests.filter((request) => request.method !== "GET"), []);
    assert.deepEqual(external, []);
    assert.equal(exceptions.length, 0);
    const userAgent = await evaluate("navigator.userAgent");
    socket.close();
    return { result: "PASS", userAgent, rows: 12, sources: 16, perPopulationCoverage: "PASS", registryProvenance: "PASS", keyboardEvidence: "PASS", liveAuthRevocationPurge: "PASS", authFailClosed: "PASS", minimumContrast: contrast.minimum, contrastSensitivity: "PASS", responsiveWidths: [390, 320], reducedMotion: "PASS", nonGetRequests: 0, externalRequests: 0, runtimeExceptions: 0, screenshotPath };
  } finally {
    chrome.kill("SIGTERM");
    server.kill("SIGTERM");
    await sleep(100);
    fs.rmSync(profile, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
}

qualify().then((result) => console.log(JSON.stringify(result, null, 2))).catch((error) => {
  console.error(error.stack);
  process.exitCode = 1;
});
