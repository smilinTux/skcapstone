#!/usr/bin/env node

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function waitForFile(file) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (fs.existsSync(file)) return;
    await sleep(50);
  }
  throw new Error("Chrome did not publish DevToolsActivePort");
}

async function qualify() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-f13-cdp-"));
  const chromePath = process.env.CHROME_PATH || "/usr/bin/google-chrome";
  const chrome = spawn(
    chromePath,
    [
      "--headless=new",
      "--no-sandbox",
      "--disable-gpu",
      "--remote-debugging-port=0",
      "--user-data-dir=" + profile,
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  try {
    const activePort = path.join(profile, "DevToolsActivePort");
    await waitForFile(activePort);
    const port = fs.readFileSync(activePort, "utf8").trim().split("\n")[0];
    const targets = await fetch("http://127.0.0.1:" + port + "/json/list").then((response) =>
      response.json(),
    );
    const pageTarget = targets.find(
      (target) => target.type === "page" && target.url === "about:blank",
    );
    assert.ok(pageTarget, "Chrome page target is unavailable");
    const socket = new WebSocket(pageTarget.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.onopen = resolve;
      socket.onerror = reject;
    });

    let nextId = 0;
    let requests = [];
    const exceptions = [];
    const pending = new Map();
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) {
        const { resolve, reject } = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result);
      }
      if (message.method === "Network.requestWillBeSent") {
        requests.push(message.params.request);
      }
      if (message.method === "Runtime.exceptionThrown") {
        exceptions.push(message.params.exceptionDetails);
      }
    };
    const send = (method, params = {}) =>
      new Promise((resolve, reject) => {
        nextId += 1;
        pending.set(nextId, { resolve, reject });
        socket.send(JSON.stringify({ id: nextId, method, params }));
      });
    const evaluate = async (expression) => {
      const response = await send("Runtime.evaluate", { expression, returnByValue: true });
      assert.equal(response.exceptionDetails, undefined);
      return response.result.value;
    };

    await send("Page.enable");
    await send("Runtime.enable");
    await send("Network.enable");

    const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
    const wireframe = new URL(
      "file://" + path.join(repo, "docs/wireframes/control-plane-estate-pulse-v2.2.html"),
    ).href;
    async function inspect(query) {
      requests = [];
      const url = wireframe + "?" + query;
      const expectedQuery = new URLSearchParams(query);
      const expectedPreview = expectedQuery.get("preview");
      const expectedState = expectedQuery.get("state");
      await send("Page.navigate", { url });
      for (let attempt = 0; attempt < 100; attempt += 1) {
        await sleep(25);
        const response = await send("Runtime.evaluate", {
          expression:
            "new URLSearchParams(location.search).get('preview') === " +
            JSON.stringify(expectedPreview) +
            " && new URLSearchParams(location.search).get('state') === " +
            JSON.stringify(expectedState) +
            " && document.readyState === 'complete' && !!document.getElementById('authorize')",
          returnByValue: true,
        });
        if (response.result.value === true) break;
        if (attempt === 99) {
          const observed = await evaluate(
            "JSON.stringify({ href: location.href, ready: document.readyState, authorize: !!document.getElementById('authorize') })",
          );
          throw new Error("Timed out waiting for the wireframe document: " + observed);
        }
      }
      const result = JSON.parse(
        await evaluate(
          "JSON.stringify((() => { const b = document.getElementById('authorize'); return { status: document.getElementById('auth-status-text').textContent, selected: document.getElementById('preview-state').value, disabled: b.disabled, ariaDisabled: b.getAttribute('aria-disabled') }; })())",
        ),
      );
      return { result, requests: [...requests] };
    }

    const closedQueries = [
      "preview=1&state=unsupported-state",
      "preview=1",
      "preview=1&state=",
      "preview=1&state=%20%20",
    ];
    for (const query of closedQueries) {
      const { result } = await inspect(query);
      assert.deepEqual(result, {
        status: "Preview state unavailable",
        selected: "unavailable",
        disabled: true,
        ariaDisabled: "true",
      });
    }

    const declared = {
      unavailable: ["Preview state unavailable", true],
      ready: ["Ready for human authorization", false],
      "stale-target": ["Stale target", true],
      "denied-policy": ["Denied by policy", true],
      expired: ["Expired preview", true],
      "changed-parameters": ["Changed parameters", true],
    };
    for (const [state, [status, disabled]] of Object.entries(declared)) {
      const { result } = await inspect("preview=1&state=" + state);
      assert.equal(result.status, status);
      assert.equal(result.selected, state);
      assert.equal(result.disabled, disabled);
      assert.equal(result.ariaDisabled, String(disabled));
    }

    await inspect("");
    const trigger = JSON.parse(
      await evaluate(
        "JSON.stringify((() => { document.querySelector('.preview').click(); const b = document.getElementById('authorize'); return { status: document.getElementById('auth-status-text').textContent, disabled: b.disabled, ariaDisabled: b.getAttribute('aria-disabled') }; })())",
      ),
    );
    assert.deepEqual(trigger, {
      status: "Ready for human authorization",
      disabled: false,
      ariaDisabled: "false",
    });

    requests = [];
    await evaluate("document.getElementById('authorize').click()");
    await sleep(100);
    const clickResult = JSON.parse(
      await evaluate(
        "JSON.stringify({ toast: document.getElementById('toast').textContent, drawerHidden: document.getElementById('auth-drawer').hidden })",
      ),
    );
    assert.equal(clickResult.drawerHidden, true);
    assert.match(clickResult.toast, /not authorized or queued/);
    assert.deepEqual(requests.filter((request) => request.method !== "GET"), []);
    assert.deepEqual(requests.filter((request) => !request.url.startsWith("file:")), []);
    assert.equal(exceptions.length, 0);

    const userAgent = await evaluate("navigator.userAgent");
    socket.close();
    return {
      result: "PASS",
      userAgent,
      failClosedUrlCases: closedQueries.length,
      declaredStates: Object.keys(declared).length,
      explicitReadyTrigger: "PASS",
      nonGetRequestsAfterClick: 0,
      externalRequestsAfterClick: 0,
      runtimeExceptions: 0,
    };
  } finally {
    chrome.kill("SIGTERM");
    await sleep(100);
    fs.rmSync(profile, { recursive: true, force: true });
  }
}

qualify()
  .then((result) => console.log(JSON.stringify(result, null, 2)))
  .catch((error) => {
    console.error(error.stack);
    process.exitCode = 1;
  });
