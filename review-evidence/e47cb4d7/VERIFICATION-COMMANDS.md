# Verification commands run during e47cb4d7 review

All commands were run from chiap03 as user skuser01.

## Git verification

```bash
cd /home/skuser01/work/skcapstone
git worktree add /home/skuser01/.skcapstone/fleet/workspaces/pi-glm-chiap03-e47cb4d7/repo -b review/e47cb4d7-independent-review main
cd /home/skuser01/.skcapstone/fleet/workspaces/pi-glm-chiap03-e47cb4d7/repo
git fetch origin
git checkout 73d5e294ab4b7e5d450375a983978b4e76e1107b
git rev-parse HEAD^{tree}
git ls-remote origin refs/heads/main refs/tags/v0.15.90
sha256sum pyproject.toml src/skcapstone/cli/coord.py tests/test_cli_coord_deps.py
```

## Wheel download and verification

```bash
cd /tmp/e47cb4d7-verify
wget https://files.pythonhosted.org/packages/79/a9/7714b40bf994fb41e6994c6721ee87298ab1bf8b9406d6803b4e9277a54e/skcapstone-0.15.90-py3-none-any.whl
wget https://files.pythonhosted.org/packages/52/4c/f83c74788c6373922abbc097838268b148df57501be36ec7b6a7fbcce006/skcoord-0.1.53-py3-none-any.whl
sha256sum skcapstone-0.15.90-py3-none-any.whl skcoord-0.1.53-py3-none-any.whl
```

## Wheel internal hash verification

```bash
cd /tmp/e47cb4d7-verify
unzip -q skcapstone-0.15.90-py3-none-any.whl skcapstone/cli/coord.py
unzip -q skcoord-0.1.53-py3-none-any.whl skcoord/card_store.py skcoord/coordination.py
sha256sum skcapstone/cli/coord.py skcoord/card_store.py skcoord/coordination.py
```

## Upstream test execution

```bash
cd /tmp/e47cb4d7-verify
PYTHONPATH=/tmp/e47cb4d7-verify/skcapstone-0.15.90-py3-none-any.whl:/tmp/e47cp4d7-verify/skcoord-0.1.53-py3-none-any.whl python -m pytest -q /home/skuser01/.skcapstone/fleet/workspaces/pi-glm-chiap03-e47cb4d7/repo/tests/test_cli_coord_deps.py -k release_claim --tb=short
```

## Semantic probe execution

```bash
cd /tmp/e47cp4d7-verify
cp /home/skuser01/.skcapstone/evidence/work/b26f9f00/20260829T004235Z/revision_release_probe.py .
sha256sum revision_release_probe.py
rm -rf isolated-test-home
PYTHONPATH=/tmp/e47cp4d7-verify/skcapstone-0.15.90-py3-none-any.whl:/tmp/e47cp4d7-verify/skcoord-0.1.53-py3-none-any.whl python revision_release_probe.py /tmp/e47cp4d7-verify/isolated-test-home
```

## Host baseline verification (executed for each host: chiap01, chiap02, chiap03, chiap04, chiap08)

```bash
# CLI hash
ssh $host "sha256sum /home/skuser01/.skenv/bin/skcapstone"

# CLI version
ssh $host "/home/skuser01/.skenv/bin/skcapstone --version"

# SKCoord version (THIS REVEALED THE DISCREPANCY)
ssh $host "python -c 'import skcoord; print(skcoord.__version__)'"

# SKCoord source hashes
ssh $host "sha256sum /home/skuser01/.skenv/lib/python3.*/site-packages/skcoord/card_store.py /home/skuser01/.skenv/lib/python3.*/site-packages/skcoord/coordination.py"

# SKCapstone source hash
ssh $host "sha256sum /home/skuser01/.skenv/lib/python3.*/site-packages/skcapstone/cli/coord.py"

# Launcher hash
ssh $host "sha256sum /home/skuser01/.local/bin/skfleet-rotate.py"

# Launcher mode
ssh $host "stat -c '%a' /home/skuser01/.local/bin/skfleet-rotate.py"

# Launcher expected-claim-revision count
ssh $host "grep -c expected-claim-revision /home/skuser01/.local/bin/skfleet-rotate.py"

# CLI help for expected option
ssh $host "/home/skuser01/.skenv/bin/skcapstone coord release-claim --help"

# Service unit hash
ssh $host "sha256sum /home/skuser01/.config/systemd/user/skfleet-rotate.service"

# Timer unit hash
ssh $host "sha256sum /home/skuser01/.config/systemd/user/skfleet-rotate.timer"

# Drop-in hash
ssh $host "sha256sum /home/skuser01/.config/systemd/user/skfleet-rotate.service.d/keep-workers.conf"
```

## Rotation log verification

```bash
sha256sum /home/skuser01/.skcapstone/evidence/fleet-rotation/20260829T001600Z/actions.log
sha256sum /home/skuser01/.skcapstone/evidence/fleet-rotation/20260829T003103Z/actions.log
cat /home/skuser01/.skcapstone/evidence/fleet-rotation/20260829T001600Z/actions.log
cat /home/skuser01/.skcapstone/evidence/fleet-rotation/20260829T003103Z/actions.log
```
