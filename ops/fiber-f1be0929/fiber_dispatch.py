"""Single-authority launch lifecycle, separate from provider/stage execution.

Transport adapters must return exact unit identity and invocation receipts.
Missing units and timeouts are ambiguous, never permission to start again.
"""
from contextlib import contextmanager, closing
import fcntl
import json
import re
from pathlib import Path
import sqlite3
import time

from fiber_admission import Refused, SPACING, reserve


@contextmanager
def authority(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'authority.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused('authority-busy') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS runs (card TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, host TEXT NOT NULL, unit TEXT NOT NULL, binding TEXT NOT NULL, state TEXT NOT NULL, invocation TEXT, receipt TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS clock (id INTEGER PRIMARY KEY CHECK(id=1), last_start REAL NOT NULL)')
    db.commit()
    return db


def _same_launch(run, observed):
    return (isinstance(observed, dict)
            and observed.get('card') == run['card']
            and observed.get('token') == run['token']
            and observed.get('host') == run['host']
            and observed.get('unit') == run['unit']
            and isinstance(observed.get('invocation'), str)
            and re.fullmatch('[0-9a-f]{32}', observed['invocation']) is not None
            and (not run['invocation'] or run['invocation'] == observed['invocation']))


def reconcile(directory, observe, *, clock=time.time):
    """Reconcile only exact, live-observed remote unit generations.

    Caller holds authority(). Adapter observe(run) must query the remote
    service manager now, not return a cached status file. Terminal requires
    a known exit code and no live main process. Keep the immutable run record.
    """
    database = Path(directory) / 'admission.sqlite3'
    results = []
    with closing(connect(database)) as db:
        runs = db.execute("SELECT * FROM runs WHERE state != 'terminal'").fetchall()
        for run in runs:
            try:
                observation = observe(dict(run))
            except Exception:
                results.append({'card': run['card'], 'state': 'unknown'})
                continue
            if not _same_launch(run, observation):
                results.append({'card': run['card'], 'state': 'unknown'})
                continue
            state = observation.get('state')
            if state == 'running':
                # Recovery waits a fresh full interval; never infer a start
                # timestamp from an absent, stale, or different-host clock.
                if run['state'] != 'running':
                    db.execute('INSERT OR REPLACE INTO clock VALUES (1, ?)', (clock(),))
                db.execute("UPDATE runs SET state='running', invocation=?, receipt=? WHERE card=?",
                           (observation['invocation'], json.dumps(observation, sort_keys=True), run['card']))
                db.commit()
                results.append({'card': run['card'], 'state': 'running'})
            elif state == 'terminal' and observation.get('main_pid') == 0 and type(observation.get('exit_code')) is int:
                # Even a very short failed job consumes the launch interval.
                if run['state'] != 'running':
                    db.execute('INSERT OR REPLACE INTO clock VALUES (1, ?)', (clock(),))
                db.execute("UPDATE runs SET state='terminal', invocation=?, receipt=? WHERE card=?",
                           (observation['invocation'], json.dumps(observation, sort_keys=True), run['card']))
                db.execute('DELETE FROM reservations WHERE card=? AND token=?', (run['card'], run['token']))
                db.commit()
                results.append({'card': run['card'], 'state': 'terminal'})
            else:
                results.append({'card': run['card'], 'state': 'unknown'})
    return results


def dispatch_one(directory, job, collect, launch, observe, *, clock=time.time, hold_file=None):
    """Admit and start at most one reviewed job, preserving ambiguous launches.

    collect() supplies a fresh, authoritative full-estate snapshot and exact
    route qualification. launch(job, receipt) is called once only; it must
    claim/read back before workspace or task actions and apply host budgets.
    This module does not grant an adapter permission to bypass those gates.
    """
    directory = Path(directory)
    with authority(directory):
        if hold_file is not None and Path(hold_file).exists():
            raise Refused('admission-held')
        database = directory / 'admission.sqlite3'
        reconcile(directory, observe, clock=clock)
        binding = json.dumps({key: value for key, value in job.items() if key != 'claimable'}, sort_keys=True, separators=(',', ':'))
        with closing(connect(database)) as db:
            old = db.execute('SELECT * FROM runs WHERE card=?', (job['card'],)).fetchone()
            if old:
                if old['binding'] != binding:
                    raise Refused('request-binding-changed')
                return {'card': job['card'], 'state': old['state'], 'new_launch': False}
            if db.execute("SELECT 1 FROM runs WHERE state NOT IN ('running','terminal')").fetchone():
                raise Refused('unresolved-launch')
            has_reservations = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reservations'").fetchone()
            if has_reservations and db.execute('SELECT 1 FROM reservations r LEFT JOIN runs j USING(card) WHERE j.card IS NULL').fetchone():
                raise Refused('orphan-reservation')
            previous = db.execute('SELECT last_start FROM clock WHERE id=1').fetchone()
            if previous and clock() - previous['last_start'] < SPACING:
                raise Refused('actual-start-spacing')
        if job.get('claimable') is not True:
            raise Refused('card-not-claimable')
        snapshot = collect()
        receipt = reserve(database, job, snapshot, now=clock())
        receipt['unit'] = 'skfleet-fiber-' + job['card'] + '-' + receipt['token'] + '.service'
        with closing(connect(database)) as db:
            db.execute('INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)',
                       (job['card'], receipt['token'], job['host'], receipt['unit'], binding, 'launching'))
            db.commit()
        # Persist launching BEFORE any remote mutation. A crash here leaves a
        # held slot and an exact unit identity for read-only recovery.
        try:
            launched = launch(job, receipt)
        except Exception as exc:
            raise Refused('launch-unacknowledged') from exc
        with closing(connect(database)) as db:
            run = db.execute('SELECT * FROM runs WHERE card=?', (job['card'],)).fetchone()
            terminal = launched.get('state') == 'terminal' and launched.get('main_pid') == 0 and type(launched.get('exit_code')) is int
            running = launched.get('state') == 'running' and type(launched.get('main_pid')) is int and launched['main_pid'] > 0
            if not _same_launch(run, launched) or not (terminal or running):
                raise Refused('launch-receipt-invalid')
            state = 'terminal' if terminal else 'running'
            db.execute("UPDATE runs SET state=?, invocation=?, receipt=? WHERE card=?",
                       (state, launched['invocation'], json.dumps(launched, sort_keys=True), job['card']))
            if terminal:
                db.execute('DELETE FROM reservations WHERE card=? AND token=?', (job['card'], receipt['token']))
            db.execute('INSERT OR REPLACE INTO clock VALUES (1, ?)', (clock(),))
            db.commit()
        return {'card': job['card'], 'state': state, 'new_launch': True, 'unit': receipt['unit']}
