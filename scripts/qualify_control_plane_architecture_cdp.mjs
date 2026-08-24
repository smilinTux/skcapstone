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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-23-cdp-"));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-23-home-"));
  const bearerFile = path.join(home, "bearer");
  const port = 17884;
  const python = `
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import uvicorn
repo = Path(${JSON.stringify(repo)})
spec = importlib.util.spec_from_file_location("decision_fixture", repo / "tests/test_control_plane_decision_context.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
rig = module.Rig(target="/api/v1/architecture/projection")
Path(${JSON.stringify(bearerFile)}).write_text(rig.bearer)
home = Path(${JSON.stringify(home)})
now = datetime.now(timezone.utc).isoformat()
from skcoord.cmdb import CMDBManager
from skcoord.cmdb_reconcile import write_run_artifact
manager = CMDBManager(home)
host = manager.create_ci("chiap04", "host", owner="infrastructure", attributes={"environment":"production","observed_at":now,"source_authority":"fleet:chiap04","scan_id":"scan-23"})
api = manager.create_ci("Control Plane API", "service", owner="platform", node="chiap04", attributes={"environment":"production","observed_at":now,"source_authority":"systemd:chiap04","scan_id":"scan-23"})
worker = manager.create_ci("Legacy Worker", "service", node="chiap04", attributes={"environment":"production","observed_at":now,"source_authority":"systemd:chiap04","scan_id":"scan-23"}, tags=["unsupported"])
manager.add_relationship(api.id, "fixture", "runs_on", host.id, authority="observed")
manager.add_relationship(worker.id, "fixture", "depends_on", api.id, authority="declared")
manager.set_status(worker.id, "fixture", "degraded")
write_run_artifact(home, {"scan_id":"scan-23","ended_at":now,"applied":True,"completeness":{"complete":True,"collectors_expected":3,"collectors_complete":3,"collectors_unavailable":0},"collector_health":{"targets":[]},"drift":{"count":2}})
from skdashboard.control_plane_adapters import aggregate_reader
from skdashboard.dashboard_architecture import ArchitectureProjectionProvider
readers = {"skcapstone.service_release":aggregate_reader({"services":2,"releases":3},observed_at=now),"skperf.aggregate":aggregate_reader({"regressions":1,"capacity_pressure":0.72},expected=4,reporting=4,observed_at=now,watermark_data="approved-fixture")}
from skdashboard.dashboard import create_app
app = create_app(home, control_plane_decision_authorizer=rig.authorizer, control_plane_invocation_factory=rig.factory, control_plane_architecture_provider=ArchitectureProjectionProvider(readers))
uvicorn.run(app, host="127.0.0.1", port=${port}, log_level="error")
`;
  const pythonPath = [path.join(repo, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  const server = spawn(process.env.PYTHON || "python", ["-c", python], { cwd: repo, env: { ...process.env, PYTHONPATH: pythonPath }, stdio: "ignore" });
  const chrome = spawn(process.env.CHROME_PATH || "/usr/bin/google-chrome", ["--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
  try {
    await waitFor(() => fs.existsSync(bearerFile), "Bearer fixture was not created");
    await waitFor(async () => fetch(`http://127.0.0.1:${port}/control-plane/architecture`).then((response) => response.ok).catch(() => false), "Dashboard did not start");
    const activePort = path.join(profile, "DevToolsActivePort");
    await waitFor(() => fs.existsSync(activePort), "Chrome did not publish DevToolsActivePort");
    const chromePort = fs.readFileSync(activePort, "utf8").trim().split("\n")[0];
    const targets = await fetch(`http://127.0.0.1:${chromePort}/json/list`).then((response) => response.json());
    const socket = new WebSocket(targets.find((target) => target.type === "page").webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    let id = 0;
    const pending = new Map(), requests = [], exceptions = [];
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) { const handlers = pending.get(message.id); pending.delete(message.id); message.error ? handlers.reject(new Error(JSON.stringify(message.error))) : handlers.resolve(message.result); }
      if (message.method === "Network.requestWillBeSent") requests.push(message.params.request);
      if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
    };
    const send = (method, params = {}) => new Promise((resolve, reject) => { id += 1; pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params })); });
    const evaluate = async (expression) => { const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }); assert.equal(result.exceptionDetails, undefined); return result.result.value; };
    const key = async (value, code, keyCode) => { await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: value, code, windowsVirtualKeyCode: keyCode }); await send("Input.dispatchKeyEvent", { type: "keyUp", key: value, code, windowsVirtualKeyCode: keyCode }); };
    await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable"); await send("Accessibility.enable");
    await send("Network.setExtraHTTPHeaders", { headers: { Authorization: `Bearer ${fs.readFileSync(bearerFile, "utf8")}`, Origin: "https://10.0.0.139:7778" } });
    await send("Page.navigate", { url: `http://127.0.0.1:${port}/control-plane/architecture?role=architect&scope=estate&window=latest&baseline=none&service=all&environment=all` });
    await waitFor(async () => evaluate("document.querySelectorAll('#architecture-metric-rows tr').length === 15").catch(() => false), "Architecture metrics did not render");
    const view = JSON.parse(await evaluate("JSON.stringify({metrics:document.querySelectorAll('#architecture-metric-rows tr').length,nodes:document.querySelectorAll('#architecture-node-rows tr').length,edges:document.querySelectorAll('#architecture-edge-rows tr').length,exceptions:document.querySelectorAll('#architecture-exception-rows tr').length,text:document.body.innerText})"));
    assert.equal(view.metrics, 15); assert.equal(view.nodes, 3); assert.equal(view.edges, 2); assert.ok(view.exceptions >= 1);
    assert.match(view.text, /Deployment frequency[\s\S]{0,80}Unknown/i); assert.match(view.text, /Approved benchmark regressions[\s\S]{0,80}1 regressions/i); assert.match(view.text, /Approved aggregate capacity pressure[\s\S]{0,80}0.72 ratio/i); assert.match(view.text, /Legacy Worker/i); assert.match(view.text, /2 dependents/i);
    await evaluate("document.querySelector('#architecture-exception-rows [data-ci]').focus()"); await key("Enter", "Enter", 13);
    assert.equal(await evaluate("document.getElementById('architecture-detail').open"), true);
    assert.match(await evaluate("document.getElementById('architecture-detail-body').innerText"), /Blast radius/i);
    await key("Escape", "Escape", 27);
    assert.equal(await evaluate("document.activeElement.hasAttribute('data-ci')"), true);
    await evaluate("document.getElementById('architecture-role').focus()"); await key("ArrowDown", "ArrowDown", 40); await key("Enter", "Enter", 13);
    await waitFor(async () => evaluate("new URL(location.href).searchParams.get('role') === 'operator'").catch(() => false), "Keyboard role change did not synchronize");
    for (const width of [390, 320]) { await send("Emulation.setDeviceMetricsOverride", { width, height: 800, deviceScaleFactor: 1, mobile: true }); const size = JSON.parse(await evaluate("JSON.stringify({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})")); assert.equal(size.scroll <= size.client, true, JSON.stringify(size)); }
    const writes = requests.filter((request) => !["GET", "OPTIONS"].includes(request.method));
    const external = requests.filter((request) => !request.url.startsWith(`http://127.0.0.1:${port}/`) && !request.url.startsWith("data:"));
    assert.deepEqual(writes, []); assert.deepEqual(external, []); assert.deepEqual(exceptions, []);
    console.log("SKCP-23 CDP PASS: 15 metrics, 3 CIs, 2 edges, exception-to-CI detail, keyboard, 390/320, zero writes/external/exceptions");
    socket.close();
  } finally { server.kill("SIGTERM"); chrome.kill("SIGTERM"); }
}

qualify().catch((error) => { console.error(error); process.exitCode = 1; });
