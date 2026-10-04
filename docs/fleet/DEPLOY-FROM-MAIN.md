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

**5. Deploy from main with `skcapstone fleet rollout`.** Dry run first, then
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
