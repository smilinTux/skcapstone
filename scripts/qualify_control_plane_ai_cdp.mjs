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

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const home = fs.mkdtempSync(path.join(os.tmpdir(), "skcp24-ai-home-"));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skcp24-ai-cdp-"));
const portFile = path.join(home, "server.port");
const python = `
import asyncio
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch
import uvicorn
from skdashboard.control_plane_adapters import Reader, project_estate as real_project
from skdashboard.dashboard import create_app
repo = Path(${JSON.stringify(repo)})
fixture = json.loads((repo / "tests/fixtures/control_plane_full_estate.v1.0.0.json").read_text())
readers = {}
for case in fixture["estate_cases"]:
    readers[case["adapter_id"]] = Reader(failure=case["failure"]) if case.get("failure") else Reader(payload={
        "schema_version": fixture["schema_version"], "observed_at": case.get("observed_at", fixture["observed_at"]),
        "watermark": case["watermark"], "coverage": case["coverage"], "aggregate": case["aggregate"],
        "errors": case["errors"], "has_observations": case["has_observations"],
    })
now = datetime.fromisoformat(fixture["projected_at"].replace("Z", "+00:00"))
patch("skdashboard.control_plane_adapters.default_readers", return_value=readers).start()
patch("skdashboard.control_plane_adapters.project_estate", side_effect=lambda value: real_project(value, now=now)).start()
app = create_app(Path(${JSON.stringify(home)}), control_plane_authorizer=lambda bearer, capability, target: bearer == "skcp24-cdp" and capability == "skdashboard.read")
counts = {"operator": 0, "architect": 0, "project-manager": 0}
async def deny(send, status, code):
    body = json.dumps({"code": code, "message": "synthetic qualification denial", "retryable": False, "request_id": code.lower()}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
class Boundary:
    async def __call__(self, scope, receive, send):
        if scope.get("path") != "/api/v1/overview":
            await app(scope, receive, send); return
        role = parse_qs(scope.get("query_string", b"").decode()).get("role", ["operator"])[0]
        counts[role] = counts.get(role, 0) + 1
        if role == "project-manager":
            await deny(send, 401, "UNAUTHENTICATED"); return
        if role == "architect" and counts[role] > 1:
            await deny(send, 403, "FORBIDDEN"); return
        if role == "architect" or (role == "operator" and counts[role] > 1):
            await asyncio.sleep(0.35)
        await app(scope, receive, send)
async def serve():
    server = uvicorn.Server(uvicorn.Config(Boundary(), host="127.0.0.1", port=0, log_level="error"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            await task
        await asyncio.sleep(0.01)
    Path(${JSON.stringify(portFile)}).write_text(str(server.servers[0].sockets[0].getsockname()[1]))
    await task
asyncio.run(serve())
`;
const server = spawn(process.env.PYTHON || "python", ["-c", python], {
  cwd: repo,
  env: { ...process.env, PYTHONPATH: path.join(repo, "src") },
  stdio: "ignore",
});
const chrome = spawn(process.env.CHROME_PATH || "/usr/bin/google-chrome", [
  "--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0",
  `--user-data-dir=${profile}`, "about:blank",
], { stdio: "ignore" });

let stage = "dashboard startup";
let result;
const stop = async (child) => {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const exited = new Promise((resolve) => child.once("exit", resolve));
  child.kill("SIGTERM");
  await Promise.race([exited, sleep(2000)]);
  if (child.exitCode === null && child.signalCode === null) {
    child.kill("SIGKILL");
    await exited;
  }
};

try {
  await waitFor(() => fs.existsSync(portFile), "Dashboard did not publish its assigned port");
  const port = Number(fs.readFileSync(portFile, "utf8"));
  assert.ok(Number.isInteger(port) && port > 0 && port < 65536, "Dashboard published an invalid port");
  await waitFor(async () => fetch(`http://127.0.0.1:${port}/control-plane/ai`).then((response) => response.ok).catch(() => false), "Dashboard did not start");
  stage = "Chrome startup";
  const active = path.join(profile, "DevToolsActivePort");
  await waitFor(() => fs.existsSync(active), "Chrome did not start");
  const chromePort = fs.readFileSync(active, "utf8").trim().split("\n")[0];
  const targets = await fetch(`http://127.0.0.1:${chromePort}/json/list`).then((response) => response.json());
  const socket = new WebSocket(targets.find((target) => target.type === "page").webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let id = 0;
  const pending = new Map();
  const requests = [];
  const exceptions = [];
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const handlers = pending.get(message.id); pending.delete(message.id);
      if (message.error) handlers.reject(new Error(JSON.stringify(message.error)));
      else handlers.resolve(message.result);
    }
    if (message.method === "Network.requestWillBeSent") requests.push(message.params.request);
    if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    id += 1; pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    assert.equal(result.exceptionDetails, undefined); return result.result.value;
  };
  const key = (value, code = value, modifiers = 0) => {
    const virtual = value === "Tab" ? 9 : value === "Enter" ? 13 : value === "Escape" ? 27 : 0;
    const text = value === "Enter" ? { text: "\r", unmodifiedText: "\r" } : {};
    return send("Input.dispatchKeyEvent", { type: "keyDown", key: value, code, modifiers, windowsVirtualKeyCode: virtual, nativeVirtualKeyCode: virtual, ...text }).then(() => send("Input.dispatchKeyEvent", { type: "keyUp", key: value, code, modifiers, windowsVirtualKeyCode: virtual, nativeVirtualKeyCode: virtual }));
  };
  const selectRole = (role) => evaluate(`(() => { const node=document.getElementById("ai-role"); node.value=${JSON.stringify(role)}; node.dispatchEvent(new Event("change",{bubbles:true})); })()`);
  const purged = () => evaluate("document.querySelectorAll('.ai-lane').length === 0 && !document.getElementById('ai-lanes').innerText.includes('1200')");

  await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable"); await send("Accessibility.enable");
  await send("Network.setExtraHTTPHeaders", { headers: { Authorization: "Bearer skcp24-cdp", Origin: "https://10.0.0.139:7778" } });
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  stage = "initial render";
  await send("Page.navigate", { url: `http://127.0.0.1:${port}/control-plane/ai?role=operator&scope=estate&window=latest&baseline=none&service=all` });
  await waitFor(async () => evaluate("document.querySelectorAll('.ai-lane').length === 3").catch(() => false), "Initial lanes did not render");
  assert.equal(await evaluate("document.querySelectorAll('#ai-quality-rows tr').length"), 11);
  assert.match(await evaluate("document.getElementById('ai-registry').innerText"), /Metric registry 1\.0\.0 \| sha256:[0-9a-f]{64}/);
  assert.equal(await evaluate("document.querySelectorAll('.ai-detail-button').length"), 10);
  const contrast = async () => evaluate(`(() => {
    const rgb = (value) => (value.match(/[\\d.]+/g) || []).map(Number);
    const lum = (value) => { const parts=rgb(value).slice(0,3).map((part)=>part/255).map((part)=>part<=.04045?part/12.92:((part+.055)/1.055)**2.4); return .2126*parts[0]+.7152*parts[1]+.0722*parts[2]; };
    const background = (node) => { for(let current=node;current;current=current.parentElement){const value=getComputedStyle(current).backgroundColor,parts=rgb(value);if(parts.length===3||(parts.length>3&&parts[3]>0))return value;}return 'rgb(255,255,255)'; };
    const ratio = (node) => { const a=lum(getComputedStyle(node).color),b=lum(background(node));return (Math.max(a,b)+.05)/(Math.min(a,b)+.05); };
    const nodes=[...document.querySelectorAll('.now-head p,.project-context label,.project-context p,.project-boundary p,.ai-lane p,.ai-measures dt,.ai-measures dd,.project-panel th,.project-panel td,.ai-registry,.quality-preview-button')].filter((node)=>node.offsetParent!==null);
    return Math.min(...nodes.map(ratio));
  })()`);
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
  const lightContrast = await contrast(); assert.ok(lightContrast >= 4.5, `light contrast ${lightContrast}`);
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
  const darkContrast = await contrast(); assert.ok(darkContrast >= 4.5, `dark contrast ${darkContrast}`);
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }, { name: "prefers-reduced-motion", value: "reduce" }] });
  assert.equal(await evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches && getComputedStyle(document.documentElement).scrollBehavior === 'auto'"), true);
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 900, deviceScaleFactor: 1, mobile: true });
  assert.ok(await evaluate("document.documentElement.scrollWidth <= innerWidth && document.querySelectorAll('.ai-lane').length === 3"));
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });

  const tree = await send("Accessibility.getFullAXTree");
  assert.ok(tree.nodes.some((node) => node.role?.value === "heading" && node.name?.value === "Outcome and evaluation quality"));
  assert.ok(tree.nodes.some((node) => node.role?.value === "button" && node.name?.value === "Open Model drilldown"));
  await evaluate("document.querySelector('.ai-detail-button').focus()");
  await key("Tab");
  assert.equal(await evaluate("document.activeElement.dataset.dimension"), "Client");
  await key("Enter");
  assert.equal(await evaluate("document.getElementById('ai-detail').open"), true);
  assert.match(await evaluate("document.getElementById('ai-detail-body').innerText"), /Metric registry 1\.0\.0/);
  await key("Escape");
  assert.equal(await evaluate("document.getElementById('ai-detail').open"), false);
  assert.equal(await evaluate("document.activeElement.dataset.dimension"), "Client");
  const screenshot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  fs.writeFileSync(path.join(home, "ai-outcomes.png"), screenshot.data, "base64");

  stage = "delayed authorized purge";
  await selectRole("architect");
  assert.equal(await purged(), true, "Prior authorized DOM survived a delayed fetch");
  await waitFor(async () => evaluate("document.querySelectorAll('.ai-lane').length === 3").catch(() => false), "Delayed authorized response did not render");

  stage = "401 purge";
  await selectRole("project-manager");
  try {
    await waitFor(async () => evaluate("document.getElementById('ai-status').innerText === 'UNAVAILABLE'").catch(() => false), "401 did not fail closed");
  } catch (error) {
    error.message += `: ${await evaluate("JSON.stringify({status:document.getElementById('ai-status').innerText,url:location.href,lanes:document.getElementById('ai-lanes').innerText})")}`;
    throw error;
  }
  assert.equal(await purged(), true, "401 retained protected DOM");

  stage = "stale response purge";
  await selectRole("operator");
  assert.equal(await purged(), true, "Reload did not purge protected DOM");
  await evaluate(`history.pushState({}, "", "/control-plane/ai?role=operator&scope=estate&window=latest&baseline=none&service=all&selected_silo=ai"); window.dispatchEvent(new PopStateEvent("popstate"));`);
  await sleep(500);
  assert.equal(await purged(), true, "Invalid popstate allowed an older response to repaint");

  stage = "403 purge";
  await evaluate(`history.replaceState({}, "", "/control-plane/ai?role=operator&scope=estate&window=latest&baseline=none&service=all")`);
  await selectRole("architect");
  await waitFor(async () => evaluate("document.getElementById('ai-status').innerText === 'UNAVAILABLE'").catch(() => false), "403 did not fail closed");
  assert.equal(await purged(), true, "403 retained protected DOM");

  await send("Emulation.setDeviceMetricsOverride", { width: 320, height: 900, deviceScaleFactor: 1, mobile: true });
  assert.ok(await evaluate("document.documentElement.scrollWidth <= innerWidth"));
  assert.equal(exceptions.length, 0, JSON.stringify(exceptions));
  assert.equal(requests.filter((request) => request.method !== "GET").length, 0);
  assert.equal(requests.filter((request) => !request.url.startsWith(`http://127.0.0.1:${port}/`)).length, 0);
  result = { result: "PASS", base: "7299700a", budgetRows: 11, registry: true, keyboard: true, focusReturn: true, axNames: true, lightContrast, darkContrast, reducedMotion: true, responsive: [390, 320], delayedPurge: true, unauthorizedPurge: true, forbiddenPurge: true, staleResponseBlocked: true, requests: requests.length, writes: 0, external: 0, exceptions: 0 };
  socket.close();
} catch (error) {
  console.error(JSON.stringify({ stage, error: error instanceof Error ? error.message : String(error), serverExit: server.exitCode, chromeExit: chrome.exitCode }));
  throw error;
} finally {
  await Promise.all([stop(server), stop(chrome)]);
  fs.rmSync(home, { recursive: true, force: true });
  fs.rmSync(profile, { recursive: true, force: true });
}
assert.equal(fs.existsSync(home) || fs.existsSync(profile), false, "Qualification scratch survived cleanup");
console.log(JSON.stringify({ ...result, scratchCleaned: true }));
