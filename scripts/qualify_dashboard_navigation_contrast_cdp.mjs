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

async function qualify() {
  const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skdash-nav-contrast-cdp-"));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "skdash-nav-contrast-home-"));
  const port = 18000 + (process.pid % 1000);
  const python = `
from pathlib import Path
import uvicorn
from skdashboard.dashboard import create_app
uvicorn.run(create_app(Path(${JSON.stringify(home)})), host="127.0.0.1", port=${port}, log_level="error")
`;
  const pythonPath = [path.join(repo, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  const server = spawn(process.env.PYTHON || "python", ["-c", python], {
    cwd: repo,
    env: { ...process.env, PYTHONPATH: pythonPath },
    stdio: "ignore",
  });
  const chrome = spawn(process.env.CHROME_PATH || "/usr/bin/google-chrome", [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--remote-debugging-port=0",
    `--user-data-dir=${profile}`,
    "about:blank",
  ], { stdio: "ignore" });
  try {
    await waitFor(
      async () => fetch(`http://127.0.0.1:${port}/board`).then((response) => response.ok).catch(() => false),
      "Dashboard did not start",
    );
    const activePort = path.join(profile, "DevToolsActivePort");
    await waitFor(() => fs.existsSync(activePort), "Chrome did not publish DevToolsActivePort");
    const chromePort = fs.readFileSync(activePort, "utf8").trim().split("\n")[0];
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
        const handlers = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) handlers.reject(new Error(JSON.stringify(message.error)));
        else handlers.resolve(message.result);
      }
      if (message.method === "Network.requestWillBeSent") requests.push(message.params.request);
      if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
    };
    const send = (method, params = {}) => new Promise((resolve, reject) => {
      id += 1;
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });
    const evaluate = async (expression) => {
      const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      assert.equal(result.exceptionDetails, undefined);
      return result.result.value;
    };
    await send("Page.enable");
    await send("Runtime.enable");
    await send("Network.enable");

    const surfaces = [
      ["Now", "/control-plane/now"],
      ["Portfolio", "/control-plane/portfolio"],
      ["Schedule", "/control-plane/schedule"],
      ["Board", "/board"],
      ["Cockpit", "/cockpit"],
      ["CMDB", "/cmdb"],
      ["Fleet", "/fleet"],
      ["Economy", "/economy"],
      ["Models", "/models"],
      ["Trust", "/trust"],
      ["Assistant", "/assistant"],
    ];
    const widths = [1280, 390, 320];
    const matrix = [];
    const oldColorRatios = [];
    const measure = `(() => {
      const parse = (value) => {
        if (value === 'transparent') return [0, 0, 0, 0];
        const numbers = (value.match(/[\\d.]+/g) || []).map(Number);
        if (value.startsWith('color(srgb')) {
          return [numbers[0] * 255, numbers[1] * 255, numbers[2] * 255, numbers[3] ?? 1];
        }
        return [numbers[0], numbers[1], numbers[2], numbers[3] ?? 1];
      };
      const over = (front, back) => {
        const alpha = front[3] + back[3] * (1 - front[3]);
        if (!alpha) return [0, 0, 0, 0];
        return [0, 1, 2].map((index) => (front[index] * front[3] + back[index] * back[3] * (1 - front[3])) / alpha).concat(alpha);
      };
      const background = (node) => {
        let result = [0, 0, 0, 0];
        for (let current = node; current; current = current.parentElement) {
          result = over(result, parse(getComputedStyle(current).backgroundColor));
        }
        return over(result, [255, 255, 255, 1]);
      };
      const luminance = (rgb) => {
        const parts = rgb.slice(0, 3).map((part) => part / 255).map((part) => part <= .04045 ? part / 12.92 : ((part + .055) / 1.055) ** 2.4);
        return .2126 * parts[0] + .7152 * parts[1] + .0722 * parts[2];
      };
      const links = [...document.querySelectorAll('[aria-label="Dashboard sections"] .tab')].filter((node) => node.offsetParent !== null).map((node) => {
        const foreground = parse(getComputedStyle(node).color);
        const behind = background(node);
        const first = luminance(foreground);
        const second = luminance(behind);
        return {
          name: node.textContent.trim(),
          active: node.classList.contains('active'),
          foreground: foreground.slice(0, 3).map(Math.round),
          background: behind.slice(0, 3).map(Math.round),
          ratio: (Math.max(first, second) + .05) / (Math.min(first, second) + .05),
        };
      });
      return { links, minimum: Math.min(...links.map((link) => link.ratio)) };
    })()`;

    for (const [surface, route] of surfaces) {
      await send("Page.navigate", { url: `http://127.0.0.1:${port}${route}` });
      await waitFor(
        async () => evaluate(`location.pathname === ${JSON.stringify(route)} && document.readyState === 'complete' && document.querySelectorAll('[aria-label="Dashboard sections"] .tab').length >= 11`).catch(() => false),
        `${surface} navigation did not render`,
      );
      for (const theme of ["light", "dark"]) {
        await evaluate(`document.documentElement.dataset.theme = ${JSON.stringify(theme)}`);
        for (const width of widths) {
          await send("Emulation.setDeviceMetricsOverride", { width, height: 900, deviceScaleFactor: 1, mobile: width < 600 });
          const result = JSON.parse(await evaluate(`JSON.stringify(${measure})`));
          assert.ok(result.links.length >= 11, `${surface} ${theme} ${width}`);
          assert.ok(result.minimum >= 4.5, JSON.stringify({ surface, theme, width, result }));
          matrix.push({ surface, theme, width, minimum: result.minimum, links: result.links });
        }
      }
      await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
      await evaluate("document.documentElement.dataset.theme = 'light'; const style = document.createElement('style'); style.id = 'old-nav-color'; style.textContent = '.tab.active{color:#1f8fa8!important}'; document.head.append(style)");
      const oldResult = JSON.parse(await evaluate(`JSON.stringify(${measure})`));
      const oldActive = oldResult.links.find((link) => link.active);
      assert.ok(oldActive.ratio < 4.5, JSON.stringify({ surface, oldActive }));
      oldColorRatios.push({ surface, ratio: oldActive.ratio });
      await evaluate("document.getElementById('old-nav-color').remove()");
    }

    const writes = requests.filter((request) => !["GET", "OPTIONS"].includes(request.method));
    const external = requests.filter((request) => /^https?:/.test(request.url) && !request.url.startsWith(`http://127.0.0.1:${port}/`));
    assert.deepEqual(writes, []);
    assert.deepEqual(external, []);
    assert.deepEqual(exceptions, []);
    const evidence = {
      result: "PASS",
      userAgent: await evaluate("navigator.userAgent"),
      surfaces: surfaces.length,
      matrixEntries: matrix.length,
      measuredLinks: matrix.reduce((total, entry) => total + entry.links.length, 0),
      minimumContrast: Math.min(...matrix.map((entry) => entry.minimum)),
      oldColorMaximum: Math.max(...oldColorRatios.map((entry) => entry.ratio)),
      oldColorSensitivity: "PASS",
      nonGetRequests: writes.length,
      externalRequests: external.length,
      runtimeExceptions: exceptions.length,
      matrix,
      oldColorRatios,
    };
    console.log(JSON.stringify(evidence));
    socket.close();
  } finally {
    server.kill("SIGTERM");
    chrome.kill("SIGTERM");
    await sleep(100);
    server.kill("SIGKILL");
    chrome.kill("SIGKILL");
    fs.rmSync(profile, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
}

qualify().catch((error) => { console.error(error); process.exitCode = 1; });
