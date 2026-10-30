"""sk-alert opens a real Talk thread per condition and keeps a Tables row for it.

Chef (2026-10-30): reply to an alert and have the estate's agent work on it in
that alert's thread, and keep the alerts in Nextcloud Tables so they can be
queried. Two Talk details decide whether that works at all:

* Only a post WITH ``threadTitle`` creates a thread in Talk 24. The old
  ``replyTo`` grouping just quoted the root and left everything in the main
  timeline, so there was no thread for the agent to own.
* Later posts go in with ``threadId``. A root that has been deleted makes Talk
  answer 400, and the alert must still land (in a fresh thread).

A local HTTP server stands in for the skhub, so nothing here needs a network.
"""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sk-alert"

COLUMNS = [
    {"id": 10, "title": "Alert", "type": "text"},
    {"id": 11, "title": "Status", "type": "selection",
     "selectionOptions": [{"id": 0, "label": "open"}, {"id": 1, "label": "ack"},
                          {"id": 2, "label": "resolved"}, {"id": 3, "label": "fyi"}]},
    {"id": 12, "title": "Severity", "type": "selection",
     "selectionOptions": [{"id": 0, "label": "info"}, {"id": 1, "label": "warn"},
                          {"id": 2, "label": "crit"}]},
    {"id": 13, "title": "Host", "type": "text"},
    {"id": 14, "title": "Estate", "type": "text"},
    {"id": 15, "title": "First seen", "type": "datetime"},
    {"id": 16, "title": "Last seen", "type": "datetime"},
    {"id": 17, "title": "Alerts sent", "type": "number"},
    {"id": 18, "title": "Last message", "type": "text"},
    {"id": 19, "title": "Talk thread", "type": "text"},
    {"id": 20, "title": "Resolved at", "type": "datetime"},
    {"id": 21, "title": "Key", "type": "text"},
]


class FakeHub:
    def __init__(self):
        self.calls = []
        self.next_msg = 100
        self.next_row = 500
        self.rows = {}
        self.bad_threads = set()
        self.columns_fail = False
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw) if raw else None
                except ValueError:
                    return raw.decode()

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                body = self._body()
                path = self.path.split("?", 1)[0]
                hub.calls.append((method, path, body))
                if method == "POST" and "/apps/spreed/api/v1/chat/" in path:
                    if isinstance(body, dict) and body.get("threadId") in hub.bad_threads:
                        return self._send(400, {"ocs": {"data": {"error": "reply-to"}}})
                    hub.next_msg += 1
                    return self._send(201, {"ocs": {"data": {"id": hub.next_msg}}})
                if method == "PUT" and "/threads/" in path:
                    return self._send(200, {"ocs": {"data": {}}})
                if method == "GET" and path.endswith("/columns"):
                    if hub.columns_fail:
                        return self._send(500, {})
                    return self._send(200, COLUMNS)
                if method == "GET" and path.endswith("/rows"):
                    return self._send(200, [
                        {"id": rid, "data": [{"columnId": int(k), "value": v} for k, v in d.items()]}
                        for rid, d in hub.rows.items()])
                if method == "POST" and path.endswith("/rows"):
                    hub.next_row += 1
                    hub.rows[hub.next_row] = dict(body["data"])
                    return self._send(200, {"id": hub.next_row})
                if method == "PUT" and "/rows/" in path:
                    rid = int(path.rsplit("/", 1)[1])
                    if rid not in hub.rows:
                        return self._send(500, {"message": "not found"})
                    hub.rows[rid].update(body["data"])
                    return self._send(200, {"id": rid})
                return self._send(404, {})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def chat_posts(self):
        return [b for m, p, b in self.calls if m == "POST" and "/spreed/api/v1/chat/" in p]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def hub():
    h = FakeHub()
    yield h
    h.close()


def run(home: Path, hub: FakeHub, *args: str, table: bool = False):
    cfg = home / "config"
    cfg.mkdir(exist_ok=True)
    env_lines = [
        "SKALERT_TALK_DEFAULT=1",
        "SKALERT_TALK_ROOM=room1",
        f"NEXTCLOUD_TALK_SERVER_URL={hub.url}",
        "NEXTCLOUD_TALK_USERNAME=agent",
        "NEXTCLOUD_TALK_APP_PASSWORD=pw",
        "SKALERT_ESTATE=test",
    ]
    if table:
        env_lines.append("SKALERT_TABLE_ID=4")
    (cfg / "alert.env").write_text("\n".join(env_lines) + "\n")
    e = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
        "SKCAPSTONE_HOME": str(home),
        "SKALERT_ENV_FILE": "/dev/null",
    }
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          env=e, timeout=60)


def test_first_alert_opens_a_titled_thread_and_repeats_go_inside(tmp_path, hub):
    r = run(tmp_path, hub, "-g", "cron/h/job", "--title", "cron job on h", "job failed")
    assert r.returncode == 0, r.stderr
    r = run(tmp_path, hub, "-g", "cron/h/job", "job still failing")
    assert r.returncode == 0, r.stderr
    first, second = hub.chat_posts()
    assert first.get("threadTitle") == "cron job on h"
    assert "replyTo" not in first and "threadId" not in first
    assert second.get("threadId") == 101
    assert "threadTitle" not in second


def test_thread_key_does_not_dedup(tmp_path, hub):
    """--thread groups but never suppresses; the caller owns the repeat policy."""
    for _ in range(3):
        assert run(tmp_path, hub, "-g", "k", "again").returncode == 0
    assert len(hub.chat_posts()) == 3


def test_resolve_closes_the_thread_and_next_incident_opens_a_new_one(tmp_path, hub):
    run(tmp_path, hub, "-g", "k", "--title", "disk low on h", "disk 95%")
    r = run(tmp_path, hub, "-g", "k", "--resolve", "disk ok again")
    assert r.returncode == 0, r.stderr
    renames = [(p, b) for m, p, b in hub.calls if m == "PUT" and "/threads/" in p]
    assert renames and renames[0][0].endswith("/threads/101")
    assert renames[0][1]["threadTitle"] == "✅ disk low on h"
    assert hub.chat_posts()[1].get("threadId") == 101
    run(tmp_path, hub, "-g", "k", "disk 96%")
    third = hub.chat_posts()[2]
    assert "threadId" not in third and third.get("threadTitle")


def test_resolve_without_an_open_thread_posts_flat(tmp_path, hub):
    r = run(tmp_path, hub, "-g", "never-alerted", "--resolve", "ok again")
    assert r.returncode == 0, r.stderr
    (post,) = hub.chat_posts()
    assert "threadId" not in post and "threadTitle" not in post


def test_deleted_root_reopens_a_thread_instead_of_losing_the_alert(tmp_path, hub):
    run(tmp_path, hub, "-g", "k", "first")
    hub.bad_threads.add(101)
    r = run(tmp_path, hub, "-g", "k", "second")
    assert r.returncode == 0, r.stderr
    posts = hub.chat_posts()
    assert posts[1].get("threadId") == 101          # tried the old thread
    assert posts[2].get("threadTitle")              # then opened a new one
    run(tmp_path, hub, "-g", "k", "third")
    assert hub.chat_posts()[3].get("threadId") == 102


def test_dedup_key_still_suppresses_and_resolve_clears_it(tmp_path, hub):
    run(tmp_path, hub, "-k", "once", "first")
    run(tmp_path, hub, "-k", "once", "suppressed")
    assert len(hub.chat_posts()) == 1
    run(tmp_path, hub, "-k", "once", "--resolve", "fixed")
    run(tmp_path, hub, "-k", "once", "back again")
    assert len(hub.chat_posts()) == 3


def test_table_row_is_created_updated_and_resolved(tmp_path, hub):
    run(tmp_path, hub, "-g", "cron/h/job", "--title", "cron job on h", "-l", "crit", "failed", table=True)
    assert len(hub.rows) == 1
    rid, row = next(iter(hub.rows.items()))
    assert row["10"] == "cron job on h"
    assert row["11"] == 0          # open
    assert row["12"] == 2          # crit
    assert row["17"] == 1
    assert row["21"] == "cron/h/job"
    assert row["14"] == "test"
    assert row["19"].endswith("/call/room1#message_101")

    run(tmp_path, hub, "-g", "cron/h/job", "-l", "crit", "still failing", table=True)
    assert len(hub.rows) == 1, "a repeat updates the condition's row"
    assert hub.rows[rid]["17"] == 2
    assert hub.rows[rid]["18"] == "still failing"

    run(tmp_path, hub, "-g", "cron/h/job", "--resolve", "ok again", table=True)
    assert hub.rows[rid]["11"] == 2  # resolved
    assert hub.rows[rid]["20"]
    assert hub.rows[rid]["17"] == 2, "a recovery is not another alert"

    run(tmp_path, hub, "-g", "cron/h/job", "failed again", table=True)
    assert len(hub.rows) == 2, "a new incident gets a new row"


def test_unkeyed_alert_gets_an_fyi_row_each(tmp_path, hub):
    run(tmp_path, hub, "one-off note", table=True)
    run(tmp_path, hub, "another note", table=True)
    assert len(hub.rows) == 2
    assert all(r["11"] == 3 for r in hub.rows.values())


def test_row_deleted_in_the_ui_is_recreated(tmp_path, hub):
    run(tmp_path, hub, "-g", "k", "first", table=True)
    hub.rows.clear()
    r = run(tmp_path, hub, "-g", "k", "second", table=True)
    assert r.returncode == 0, r.stderr
    assert len(hub.rows) == 1


def test_table_only_delivers_nothing(tmp_path, hub):
    r = run(tmp_path, hub, "-g", "k", "--table-only", "backfilled", table=True)
    assert r.returncode == 0, r.stderr
    assert hub.chat_posts() == []
    assert len(hub.rows) == 1


def test_table_failure_never_fails_the_alert(tmp_path, hub):
    hub.columns_fail = True
    r = run(tmp_path, hub, "-g", "k", "x", table=True)
    assert r.returncode == 0, r.stderr
    assert len(hub.chat_posts()) == 1
    log = (tmp_path / "logs" / "skalert.log").read_text()
    assert "TABLE FAILED" in log


TABLE_CLI = SCRIPT.parent / "sk-alert-table"


def test_table_cli_reads_rows_back_with_labels(tmp_path, hub):
    """Agents answer "what is open / what failed most" with sk-alert-table."""
    run(tmp_path, hub, "-g", "a", "--title", "cron a on h", "-l", "crit", "a failed", table=True)
    run(tmp_path, hub, "-g", "a", "-l", "crit", "a still failing", table=True)
    run(tmp_path, hub, "-g", "b", "--title", "disk low on h", "-l", "warn", "disk", table=True)
    run(tmp_path, hub, "-g", "b", "--resolve", "disk ok", table=True)
    e = {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"],
         "SKCAPSTONE_HOME": str(tmp_path), "SKALERT_ENV_FILE": "/dev/null"}
    r = subprocess.run(["python3", str(TABLE_CLI), "rows", "--status", "open", "--json"],
                       capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert [x["Alert"] for x in rows] == ["cron a on h"]
    assert rows[0]["Severity"] == "crit" and rows[0]["Alerts sent"] == 2
    r = subprocess.run(["python3", str(TABLE_CLI), "top", "--since", "1d", "--json"],
                       capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 0, r.stderr
    top = json.loads(r.stdout)
    assert top[0]["condition"] == "cron a on h" and top[0]["alerts_sent"] == 2
    assert {t["condition"] for t in top} == {"cron a on h", "disk low on h"}
