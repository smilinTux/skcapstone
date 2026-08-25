#!/usr/bin/env node

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitFor(check, message) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (await check()) return;
    await sleep(50);
  }
  throw new Error(message);
}

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-50-cdp-"));
const home = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-50-home-"));
const artifactDir = path.resolve(process.env.SKCP50_ARTIFACT_DIR || "/tmp/skcp50-browser");
const port = 17888;
const routes = [
  "/control-plane/now",
  "/control-plane/portfolio?role=project-manager&scope=estate&window=latest&baseline=none&service=all",
  "/control-plane/reliability?role=operator&scope=estate&window=latest&baseline=none&service=all",
  "/control-plane/architecture?role=architect&scope=estate&window=latest&baseline=none&service=all&environment=all",
  "/control-plane/ai?role=operator&scope=estate&window=latest&baseline=none&service=all",
  "/control-plane/governance?role=governance&scope=estate&window=latest&baseline=none&service=all",
  "/control-plane/reports?role=project-manager&scope=estate&window=latest&baseline=none&service=all&report_type=all",
];
const python = `from pathlib import Path\nimport uvicorn\nfrom skdashboard.dashboard import create_app\nuvicorn.run(create_app(Path(${JSON.stringify(home)})), host="127.0.0.1", port=${port}, log_level="error")`;
const pythonPath = [process.env.HOME + "/work/capauth/src", path.join(repo, "src")].join(path.delimiter);
const server = spawn(process.env.PYTHON || "python", ["-c", python], { cwd: repo, env: { ...process.env, PYTHONPATH: pythonPath }, stdio: "ignore" });
const chrome = spawn(process.env.CHROME_PATH || "/usr/bin/google-chrome", ["--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });

try {
  fs.mkdirSync(artifactDir, { recursive: true });
  await waitFor(async () => fetch(`http://127.0.0.1:${port}/control-plane/now`).then((response) => response.ok).catch(() => false), "Dashboard did not start");
  const active = path.join(profile, "DevToolsActivePort");
  await waitFor(() => fs.existsSync(active), "Chrome did not start");
  const chromePort = fs.readFileSync(active, "utf8").trim().split("\n")[0];
  const targets = await fetch(`http://127.0.0.1:${chromePort}/json/list`).then((response) => response.json());
  const socket = new WebSocket(targets.find((target) => target.type === "page").webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let id = 0;
  const pending = new Map(), requests = [], exceptions = [];
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const handlers = pending.get(message.id); pending.delete(message.id);
      message.error ? handlers.reject(new Error(JSON.stringify(message.error))) : handlers.resolve(message.result);
    }
    if (message.method === "Network.requestWillBeSent") requests.push(message.params.request);
    if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => { id += 1; pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params })); });
  const evaluate = async (expression) => {
    const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    assert.equal(result.exceptionDetails, undefined);
    return result.result.value;
  };
  await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable"); await send("Accessibility.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  const matrix = [];
  for (const route of routes) {
    await send("Page.navigate", { url: `http://127.0.0.1:${port}${route}` });
    await waitFor(async () => evaluate("document.readyState === 'complete'").catch(() => false), `Page did not load: ${route}`);
    await evaluate("document.activeElement?.blur()");
    await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
    const dom = JSON.parse(await evaluate(`JSON.stringify((() => {
      const focus = document.activeElement;
      const style = focus && getComputedStyle(focus);
      return {
        title: document.title,
        mainLandmarks: document.querySelectorAll('main,[role="main"]').length,
        navigationLandmarks: document.querySelectorAll('nav,[role="navigation"]').length,
        h1: [...document.querySelectorAll('h1')].map((node) => node.textContent.trim()),
        focusName: focus?.getAttribute('aria-label') || focus?.textContent.trim() || '',
        focusVisible: !!focus && style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) >= 2,
      };
    })())`));
    const tree = await send("Accessibility.getFullAXTree");
    const roles = tree.nodes.map((node) => node.role?.value).filter(Boolean);
    matrix.push({ route, ...dom, axMain: roles.filter((role) => role === "main").length, axNavigation: roles.filter((role) => role === "navigation").length });
    if (route === routes[0]) {
      const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      fs.writeFileSync(path.join(artifactDir, "now-desktop.png"), shot.data, "base64");
    }
  }
  const result = {
    result: matrix.every((item) => item.mainLandmarks === 1 && item.axMain === 1 && item.navigationLandmarks >= 1 && item.axNavigation >= 1 && item.focusVisible) ? "PASS" : "FAIL",
    browser: await evaluate("navigator.userAgent"), matrix,
    nonGetRequests: requests.filter((request) => !["GET", "OPTIONS"].includes(request.method)).length,
    externalHttpRequests: requests.filter((request) => /^https?:/.test(request.url) && !request.url.startsWith(`http://127.0.0.1:${port}/`)).length,
    runtimeExceptions: exceptions.length,
  };
  fs.writeFileSync(path.join(artifactDir, "accessibility-landmark-matrix.json"), JSON.stringify(result, null, 2) + "\n");
  console.log(JSON.stringify(result, null, 2));
  socket.close();
  if (result.result !== "PASS") process.exitCode = 1;
} finally {
  server.kill("SIGTERM"); chrome.kill("SIGTERM");
  await sleep(100);
  fs.rmSync(profile, { recursive: true, force: true }); fs.rmSync(home, { recursive: true, force: true });
}
