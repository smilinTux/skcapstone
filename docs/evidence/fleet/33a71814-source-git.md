# Exact builder source and host Git configuration

Card33a71814 reproduces the frontend refusal on installed42d10961. The exact
d53 qualification source stores https://skgit.skstack01.douno.it/smilinTux/sklegal.git.
Inherited host Git configuration reports ssh://git@skgit.skstack01.douno.it:222/smilinTux/sklegal.git,
so materialize_source rejects correct custody as an origin mismatch.

Verification and reconstruction now use the absolute system Git executable,
a minimal environment, no system/global Git configuration, disabled interactive
credential prompts, hooks and fsmonitor. Actual source/origin mismatch still
refuses. Existing workspaces are preserved; host configuration is not changed.
No repository, card, profile or retained source is rebound by this fix.

Before fix:3 failed,2 passed in the focused new regression tests.
After fix:93 passed across these files:

```text
env -u BASH_ENV PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 ~/.skenv/bin/python -B -m pytest tests/fleet/test_builder_source_git.py tests/fleet/test_builder_dispatch.py tests/fleet/test_worker_git.py -q -p no:cacheprovider --tb=short
```

The tests use only local synthetic Git source. Black,Ruff and git diff --check
pass. Hosted checks are required before automatic rebase merge. Rollout stays
with nor. No production candidate installation or host configuration mutation.
