# Sealed monorepo Python qualification

Card14102d48, ownerjarvis, claim81b83713c52443ac8ebf1a3477906cb7. Basebf712d90d249ec9da6474c87d793305ce2f2089d. Retained17818226 owner/source and2fcc1319 owner/d53 are unchanged. No install, services, database or live gateway changes.

The exact264 source is a monorepo. Host Python resolves sklegal_domain, sklegal_api, sklegal_migration and sklegal_hammertime to other worktrees through editable startup files. The current sealed sandbox mounts only the exact source under /work and sets PYTHONPATH=/work/src, so it cannot import264's modules under packages/**/src and services/*/src. Host import success would test the wrong bytes.

The existing executor now derives at most64 additional package/service/vendor src roots and resolves them inside its existing readonly /work mount. It refuses redirected roots and path names that could inject PYTHONPATH entries. Simple src-layout candidates keep exactly /work/src. Runtime/source mounts, clearenv, unshare-all, source/plan checks and per-file JUnit validation remain unchanged. This changes a fingerprinted executor module, so qualification must occur after merged-main rollout, not against the previous harness.

Regression before implementation:6failed1passed. After implementation:7passed, including real bubblewrap execution of a synthetic pinned module while an inherited older-host PYTHONPATH is excluded. Final coverage adds two injection refusal cases. Affected profile/executor/Node suites:174passed in4.00seconds, zero skips:

```
env -u BASH_ENV -u VIRTUAL_ENV PYTHONPATH=src /tmp/skcapstone-base-venv/bin/python -m pytest -q tests/fleet/test_monorepo_test_sandbox.py tests/fleet/test_production_tests.py tests/fleet/test_production_test_profile.py tests/fleet/test_production_test_node.py -m 'not host_systemd'
```

Black, Ruff and diff checks pass. The first source commit preceded broader verification. Final hosted CI is pending. PR932 uses auto rebase merge; lumina-nor owns main-only rollout. PR931 separately repairs the consumed terminal reservation and repeated admission folding. Neither repair is installed from a worktree.

This is source import plumbing, not a qualified profile, source rebind, acceptance verdict or worker launch. Actual native test execution with retained ownership and exact source remains mandatory. Missing runtime dependencies or genuine test failures must be reported rather than filled from older host checkouts. No namespace boundary or protected-data gate is relaxed.
