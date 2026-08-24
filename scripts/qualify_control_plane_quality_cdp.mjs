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
  for (let attempt = 0; attempt < 160; attempt += 1) {
    if (await check()) return;
    await sleep(50);
  }
  throw new Error(message);
}

async function qualify() {
  const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-14-home-"));
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-14-cdp-"));
  const port = 17878;
  const server = spawn(
    process.env.PYTHON || "python",
    ["-c", `from pathlib import Path\nimport uvicorn\nfrom skdashboard.dashboard import create_app\ndef authorize(bearer, capability, target):\n    return bearer == "quality-cdp" and capability == "skdashboard.read"\nuvicorn.run(create_app(Path(${JSON.stringify(home)}), control_plane_authorizer=authorize), host="127.0.0.1", port=${port}, log_level="error")`],
    { cwd: repo, env: { ...process.env, PYTHONPATH: path.join(repo, "src") }, stdio: "ignore" },
  );
  const chrome = spawn(
    process.env.CHROME_PATH || "/usr/bin/google-chrome",
    ["--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"],
    { stdio: "ignore" },
  );

  try {
    await waitFor(async () => fetch(`http://127.0.0.1:${port}/`).then((r) => r.ok).catch(() => false), "Dashboard did not start");
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
    await send("Network.setExtraHTTPHeaders", { headers: { Authorization: "Bearer quality-cdp", Origin: "http://10.0.0.139:7778" } });
    await send("Accessibility.enable");
    await send("Page.navigate", { url: `http://127.0.0.1:${port}/` });
    await waitFor(async () => evaluate("document.querySelectorAll('.quality-issue').length > 0 && !!document.querySelector('.tile[href=\"/cmdb\"]')").catch(() => false), "Quality and overview did not render");

    const desktop = JSON.parse(await evaluate(`JSON.stringify((() => ({
      heading: document.getElementById('quality-heading').textContent,
      states: [...document.querySelectorAll('.truth-badge')].map((node) => node.textContent.trim()),
      issues: document.querySelectorAll('.quality-issue').length,
      unavailableCoverage: [...document.querySelectorAll('.quality-issue dd')].some((node) => node.textContent === 'Coverage unavailable'),
      source: document.querySelector('.quality-issue .mono').textContent,
      previewLabel: document.querySelector('.quality-preview-button').textContent,
      cmdbTile: document.querySelector('.tile[href="/cmdb"]').textContent,
    }))())`));
    assert.equal(desktop.heading, "Data quality");
    assert.ok(desktop.issues > 0);
    assert.equal(desktop.unavailableCoverage, true);
    assert.equal(desktop.previewLabel, "Preview refresh");
    assert.match(desktop.cmdbTile, /Unknown/);
    assert.match(desktop.cmdbTile, /health unknown/);
    assert.doesNotMatch(desktop.cmdbTile, /all healthy/);
    for (const state of ["current", "stale", "partial", "unavailable", "unreachable", "unknown", "not applicable"]) {
      assert.ok(desktop.states.some((label) => label.includes(state)));
    }
    const qualityTree = await send("Accessibility.getFullAXTree");
    const accessible = qualityTree.nodes.map((node) => ({
      role: node.role && node.role.value,
      name: node.name && node.name.value,
    }));
    assert.ok(accessible.some((node) => node.role === "heading" && node.name === "Data quality"));
    assert.ok(accessible.some((node) => node.role === "button" && node.name === "Preview refresh"));

    await evaluate("document.querySelector('[data-nav=home]').focus()");
    const focusTrace = [];
    for (let attempt = 0; attempt < 30; attempt += 1) {
      if (await evaluate("document.activeElement.classList.contains('quality-preview-button')")) break;
      focusTrace.push(await evaluate("document.activeElement.tagName + ':' + document.activeElement.className"));
      await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9, nativeVirtualKeyCode: 9 });
      await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9, nativeVirtualKeyCode: 9 });
    }
    assert.equal(await evaluate("document.activeElement.classList.contains('quality-preview-button')"), true, JSON.stringify(focusTrace));
    await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", text: "\r", unmodifiedText: "\r", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
    assert.equal(await evaluate("document.getElementById('quality-preview').open"), true);
    assert.match(await evaluate("document.getElementById('quality-preview').textContent"), /does not refresh, remediate, queue, or authorize/);
    await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
    await sleep(50);
    assert.equal(await evaluate("document.getElementById('quality-preview').open"), false);
    assert.equal(await evaluate("document.activeElement.classList.contains('quality-preview-button')"), true);

    await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    const mobileColumns = await evaluate("getComputedStyle(document.getElementById('quality-issues')).gridTemplateColumns.split(' ').length");
    assert.equal(mobileColumns, 1);
    assert.equal(await evaluate("document.documentElement.scrollWidth <= innerWidth"), true);
    assert.deepEqual(requests.filter((request) => request.method !== "GET"), []);
    assert.equal(exceptions.length, 0);
    const userAgent = await evaluate("navigator.userAgent");
    socket.close();
    return { result: "PASS", userAgent, issues: desktop.issues, screenReaderTree: "PASS", keyboardDialog: "PASS", mobileColumns, nonGetRequests: 0, runtimeExceptions: 0 };
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
