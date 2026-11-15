# Deploying skcapstone: main is what runs

**The rule:** every line of skcapstone code that runs on a fleet host came
from `origin/main` on GitHub. The path is always

```
branch  ->  push  ->  PR  ->  CI green  ->  merge to main  ->  rollout from main
```

No exceptions for "it is only one file", "CI is slow", or "I will PR it
later". The one sanctioned shortcut is the emergency path below, and even
that starts from a pushed commit.

## Why this changed (2026-10-03)

For a while hot deploys were normal because getting a change merged took
hours. The cost showed up all at once on 2026-10-03:

- chiap08's `~/.skenv` ran code that existed in no repo anyone could see.
  Reconciling it back into main took a full day and an 88 commit PR (#898).
- A fleet agent committed to a repo whose `origin` was a local path, then
  hot-installed six modules over the top of that reconciliation. Its work
  and a pending feature PR (#903) now overlap on six files.
- A second session hand-installed a memory guard into the live venv while
  the first was trying to prove what the live venv contained.
- Three different `skmail` binaries sat across five hosts, and
  `pip show skcapstone` reported the same version on all of them.

The speed problem is fixed at the merge step instead (auto-merge on green,
below), so there is no longer a reason to bypass it.

## The normal path, worked example

Say you are fixing a bug in `src/skcapstone/fleet/production_resources.py`.

**1. Work in your own worktree, off fresh main.** Never in a shared checkout.

```bash
cd ~/work/skcapstone            # any clone of smilinTux/skcapstone
git fetch origin
git worktree add ~/work/wt-memory-floor -b fix/memory-floor origin/main
cd ~/work/wt-memory-floor
```

**2. Change, test the narrow thing, commit right away.** Commit before any
long run: a turn can end mid-suite, and uncommitted work dies with it.

```bash
$EDITOR src/skcapstone/fleet/production_resources.py
~/.skenv/bin/python -m pytest tests/fleet/test_production_physical_memory.py -q
printf -- '- Memory floor now counts page cache as reclaimable.\n' \
  > changelog.d/memory-floor.md
git add -A && git commit -m "fix(fleet): count page cache as reclaimable in memory floor"
```

**3. Push and open the PR with auto-merge armed.** GitHub merges it the
moment the required checks pass (docs, gitleaks, lint, shim-imports, unit
tests 3.11 and 3.12). No human has to come back to click anything.

```bash
git push -u origin fix/memory-floor
gh pr create --repo smilinTux/skcapstone --base main \
  --title "fix(fleet): count page cache in memory floor" \
  --body "Card d11d2601. Narrow test passes; full suite runs in CI."
gh pr merge --repo smilinTux/skcapstone --auto --rebase fix/memory-floor
```

Use `--rebase`. It keeps each labelled commit on main, which is what the
review and provenance tooling reads.

**4. Confirm it actually merged.** A green check is not a merge.

```bash
gh pr view fix/memory-floor --repo smilinTux/skcapstone --json state,mergeCommit
git fetch origin && git log --oneline -1 origin/main
```

If CI fails, fix on the same branch and push again. Auto-merge stays armed.

**5. Deploy from main: automatic.** On the chi estate the
`skfleet-auto-rollout.timer` on the authority host (chiap08) checks origin
every 2 minutes and, when main moved, rolls it host by host with the same
`skcapstone fleet rollout --apply` gates (canary chiap08 first), restarts
`sknoded`, and skmails jarvis and lumina-nor `ROLLED <sha>`. It stops at the
first host whose gate fails, leaves later hosts on the previous main, exits
nonzero (the `skcapstone-alert@` hook fires) and skmails `HALTED at <host>`.
You normally do nothing here: merge, then watch for the `ROLLED` mail.

Why a timer and not a person or a chat-session loop: on 2026-10-07 a merged
dispatcher fix (#970) sat undeployed for about nine hours because the loop
deploying it lived in an operator's chat session and had ended. Install and
ownership notes are in `scripts/fleet/systemd/skfleet-auto-rollout.service`.
Exactly one host runs it.

### The fleet lock

`deploy/fleet-constraints.txt` is the fleet runtime's **fully resolved lock**:
every package in the venv and every transitive dependency, at one version each,
with environment markers so one file serves every fleet Python (3.12 and 3.14).
It is generated, never hand-edited, by `scripts/fleet-lock.sh` from three inputs:

| Input | Holds |
| --- | --- |
| `deploy/fleet-requirements.in` | the SK packages, exact `==` pins, required on every host |
| `deploy/fleet-tools.in` | the third-party tools that live in the same venv (sentence-transformers, huggingface-hub, scrapling, twine, playwright, ...), unpinned; inventoried from `pip list --not-required` on the hosts |
| `deploy/fleet-self.in` | skcapstone itself (`.[fleet-qualify]`, from the checkout) |

Why: until 2026-11-15 the file held only the SK pins. PR #1382 pinned four SK
releases whose caps pulled click and typer down, which broke huggingface-hub (a
tool in the same venv), and every check was green because no check installed
the whole set. A lock that names every package makes such a conflict visible
before merge.

How the consumers use it:

- **CI** (`.github/actions/test-env`): `-c` the lock, `-r fleet-requirements.in`.
- **Rollout** (`pip_install`): `-c` the lock, `-r fleet-requirements.in`, and `-r`
  the lock lines for every package the host already has installed
  (`dependency_parity.py --write-installed`). A tool a host never had is not
  pushed onto it; a tool it has moves to the lock's version.
- **Parity gate** (`verify_dependency_parity`): an SK pin missing or different
  halts the host with `DEPENDENCY_DRIFT`; any other lock entry may be absent,
  but if installed it must match.
- **`verify_pip_check`** (new, right after parity): `pip check` on the live venv.
  A conflict halts the rollout on the first host, so the canary is the only
  host that sees it.

#### The gate on a lock PR (`.github/workflows/fleet-lock.yml`)

Every PR that touches the lock, its inputs, `deploy/dependency-caps.md` or
`pyproject.toml` must pass, in a fresh venv on Linux with Python 3.12 and 3.14:

1. `scripts/fleet-lock.sh --check`: the lock is current for its inputs;
2. install the whole lock plus `.[fleet-qualify]`;
3. `pip check`;
4. `dependency_parity.py`: what got installed is what the lock says;
5. `fleet_lock_gate.py smoke`: import every SK package and every MCP server
   module, and run every SK console script with `--help`;
6. `fleet_lock_gate.py caps`: every upper bound an SK package puts on a
   third-party library is a row in `deploy/dependency-caps.md`;
7. `fleet_lock_gate.py security-downgrades`: no security-relevant package
   (cryptography, pyOpenSSL, PyJWT, urllib3, requests, certifi, paramiko,
   Pillow, starlette, fastapi, aiohttp, h11, jinja2, PGPy) goes down compared
   with main, unless the PR body has a line `security-downgrade: <name> <why>`;
8. `pip-audit` against PyPI and OSV advisories on the resolved lock (also daily
   on main, so a new advisory against an unchanged lock turns main red).

The jobs always report (they skip their steps on a PR that touches none of
those files), so they can be required checks.

#### Changing the set

1. The upstream release ships first (a published tag; never an unreleased git
   overlay, never "one past the newest tag").
2. In the SAME PR as the code that needs it: raise the `==` pin in
   `deploy/fleet-requirements.in` (or add a tool to `deploy/fleet-tools.in`) and
   the floor in `pyproject.toml`, then run `scripts/fleet-lock.sh` and commit the
   regenerated lock. The floor may equal the pin but must never exceed it
   (`tests/test_fleet_dependency_parity.py`).
3. A new upper cap on a third-party library needs a reason and a test: add the
   row to `deploy/dependency-caps.md` or the gate fails.
4. Merge. The next auto-rollout installs the lock on each host and halts at the
   first host whose parity or `pip check` fails.

### Renovate

Every SK repo extends one shared preset, `smilinTux/renovate-config`
(`"extends": ["github>smilinTux/renovate-config"]`), so the policy lives in one
place. What it does:

- **Coverage:** Python (pyproject, requirements, uv/pip-compile locks), npm
  (package.json and lockfiles), GitHub Actions (pinned to a commit SHA),
  Dockerfiles and base images (pinned to a digest), and here the fleet lock
  (Renovate's `pip-compile` manager reruns the command in the lock's header).
- **Security first:** vulnerability alerts (GitHub and OSV) open a PR at once,
  outside the schedule and the rate limits, labelled `security`, with the
  advisory link in the body.
- **Grouping:** one weekly PR of minor updates and one of patch updates per
  repo; majors one at a time, each with its release notes; a Dependency
  Dashboard issue per repo; weekly lock-file maintenance (here: one PR that
  moves the whole fleet lock, which then faces the gate above).
- **Caps:** a requirement with an upper bound gets a monthly "try lifting this
  cap" PR when a release above it exists (label `cap-lift`). Its CI is the test:
  green means the cap goes and its row in `deploy/dependency-caps.md` goes too;
  red means the row stays and `last checked` moves.
- **Merging:** Renovate merges only patch updates and security fixes, and only
  after ALL checks are green (Renovate merges itself rather than through
  GitHub auto-merge, which waits only for required checks). For the fleet lock
  that includes resolve, `pip check`, smoke, caps, the security-downgrade check
  and the vulnerability scan. Minors and majors need a human or orchestrator
  review.
- **Rollout:** a merged lock PR is an ordinary merge to main, so it reaches the
  hosts only through the auto-rollout above: the canary first, the next host
  only after the canary's parity and `pip check` pass. Nothing goes to every
  host at once.

**5b. Deploy by hand (canary, emergencies, or with the timer stopped).** Dry run first, then
one canary host, then the rest. Rollout pulls main on each host, installs,
converges, and stops at the first host that fails its gate. Details in
[`rollout-drift.md`](rollout-drift.md) section 4.

```bash
# preview only, touches nothing
skcapstone fleet rollout --node chiap03 --repo-root ~/work/skcapstone

# canary
skcapstone fleet rollout --node chiap03 --repo-root ~/work/skcapstone --apply

# the rest, in order, halts on first failure
skcapstone fleet rollout --node chiap01 --node chiap02 --node chiap04 \
  --node chiap08 --repo-root ~/work/skcapstone --apply
```

**6. Verify what is loaded, not what is merged.**

```bash
ssh chiap03 'skcapstone fleet node drift'                 # content vs main
ssh chiap03 'systemctl --user show skfleet-rotate.service -p ActiveEnterTimestamp --value'
```

A service that started before the install is still running the old code.
Restart it (or let `rollout` converge it) before you call the fix live.

**7. Clean up.**

```bash
git worktree remove ~/work/wt-memory-floor
```

## Never do these

| Do not | Why | Do instead |
|---|---|---|
| `cp src/skcapstone/x.py ~/.skenv/lib/python3.*/site-packages/skcapstone/` | Invisible to git, overwritten by the next install, breaks drift and test profiles | Normal path above |
| `~/.skenv/bin/pip install -e ~/work/my-worktree` on a live host | The live venv now tracks an unmerged worktree; deleting the worktree breaks the host | Normal path above |
| Edit files under `site-packages/` or `~/.skenv/bin/` in place | Same as the first row | Normal path above |
| Commit to a repo whose `origin` is a local path (`git remote -v` shows `/home/...`) | Nobody else can see it, review it, or merge it | `git remote set-url origin https://github.com/smilinTux/skcapstone.git`, push your branch, open a PR |
| Hold work on a local branch "until it is ready" | One `reset` or pull and it is gone | Push the branch the same turn you commit; open it as a draft PR |
| `gh pr merge --admin` to skip checks | Admin bypass is disabled on main on purpose | Fix the check, or use the emergency path |

If you find a host already running code that is not on main, do not fix it
by installing more code on top. Push what is installed to a
`publish/installed-<host>-<date>` branch first so nothing is lost, then
reconcile it into main through a PR.

## Emergency path (host down, cannot wait for CI)

Only when a host is broken right now and the CI run would take longer than
the outage can last. It still starts from a pushed commit.

```bash
# 1. commit and push the fix, open the PR, arm auto-merge (steps 1 to 3 above)
git push -u origin hotfix/chiap08-dispatcher-crash
gh pr create --repo smilinTux/skcapstone --base main --title "hotfix: ..." --body "Emergency deploy to chiap08 at <time>, card <id>."
gh pr merge --repo smilinTux/skcapstone --auto --rebase hotfix/chiap08-dispatcher-crash

# 2. install that exact PUSHED commit on the one broken host
ssh chiap08 'cd ~/work/skcapstone && git fetch origin \
  && git checkout --detach <pushed-sha> && ~/.skenv/bin/pip install -e .'

# 3. once the PR merges, put the host back on main
skcapstone fleet rollout --node chiap08 --repo-root ~/work/skcapstone --apply
```

Record the PR number and sha on the card. Until step 3 runs, `node drift`
will report the host as drifted, which is correct: it is.

## For fleet agents

Workers and reviewers do not deploy at all. Your job ends at step 4 (PR
merged, or PR open with your verdict recorded). Rollout is run by the
operator or the orchestrator. If you think a deploy is needed, say so on
skmail with the PR number, do not install it yourself.


## Completion after merge

A source card is complete when an independent review PASS names the exact
commit, the tests are green on that same commit, and the code is merged on
`origin/main`. The operator then runs `skcapstone coord accept <source_card>
--agent <operator> --repo-path <clone>`. Deploy is a separate operator step
(`skcapstone fleet rollout`, or the chi auto-rollout timer); it is not part of
card completion.

## Sealed source-suite qualification dependencies

Governed staged deploy and rollback install `.[fleet-qualify]` as a regular package, not an editable checkout. Native test workers mount the interpreter prefix read-only; dashboard tests import `skcapstone.agent_run` and `skcapstone.fleet` from that prefix. A checkout outside the sealed mounts cannot satisfy these imports. This environment failure must not be recorded as a product known-failure baseline.

The extra declares ansible-core 2.21.3 on Python 3.12+ to match skstacks render CI, and 2.19.5 on the supported Python 3.11 floor. Missing controller/core packages are reported by readiness and drift. The scoped pytest runtime fingerprint remains unchanged; these packages do not broaden it to unrelated application bytes.

The operator owns deployment. Do not install a candidate into a live prefix. SKLegal fan-out has priority over skbackup/dashboard qualification; native tests and GLM work may start only when the existing admission boundary has capacity available for this work.
