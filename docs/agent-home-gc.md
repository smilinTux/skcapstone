# One-shot agent home garbage collection

`skcapstone-agent-home-gc` conservatively identifies stale one-shot worker homes under `~/.skcapstone/agents`.

The weekly `skcapstone-agent-home-gc.timer` is restricted to `chiap08`. Its service intentionally omits `--apply`, so the scheduled first phase is a dry run that writes `~/.skcapstone/reports/agent-home-gc/latest.json`. Review that report before enabling deletion by adding `--apply` to a local service override.

A home qualifies only when all of these conditions hold:

- its name begins with a known worker prefix;
- neither the directory nor anything beneath it has been touched in more than 30 days;
- strict folding of the complete CardStore finds no live card claim owned by that agent;
- it is not a reserved seat or template name; and
- it is a real directory rather than a symbolic link.

Unreadable CardStore data fails the entire run closed. Evidence is not beneath the agents directory. The collector never traverses or mutates `~/.skcapstone/evidence/work`.

Run an ad hoc dry run:

```console
skcapstone-agent-home-gc --report ~/.skcapstone/reports/agent-home-gc/manual.json
```

After reviewing the report, explicitly apply the same policy:

```console
skcapstone-agent-home-gc --apply --report ~/.skcapstone/reports/agent-home-gc/applied.json
```
