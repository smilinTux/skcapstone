"""Read-only audit of the user crontab: entries whose target is missing.

A crontab line whose script no longer exists fails every run and, under a bare
cron PATH, usually fails silently. ``skcapstone doctor`` uses this module to
REPORT such entries. It never writes the crontab and never removes anything.

Handled syntax: comments, blank lines, ``NAME=value`` environment lines (a
``PATH=`` line is honoured when resolving bare commands), ``@reboot`` style
schedules, ``cd x && y`` chains, ``;``/``||``/``|`` separated commands,
wrappers such as ``sk-cron-run <job> <cmd>`` (the wrapped command is checked,
not the job name), interpreter invocations (``/bin/bash script.sh``), redirects,
and ``~`` / ``$HOME`` expansion.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

#: PATH cron uses when the crontab does not set one.
DEFAULT_CRON_PATH = "/usr/bin:/bin"

_ENV_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SPECIAL = {
    "@reboot",
    "@yearly",
    "@annually",
    "@monthly",
    "@weekly",
    "@daily",
    "@midnight",
    "@hourly",
}
#: Shell builtins and keywords: nothing on disk to check.
_BUILTINS = {
    "cd", "test", "[", "[[", "exit", "true", "false", ":", "echo", "export", "set", "unset",
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac",
    "!", "{", "}", "exec", "source", ".", "eval", "umask", "ulimit", "read", "printf", "wait",
}  # fmt: skip
#: Wrappers: command name -> number of leading args to skip before the wrapped command.
_WRAPPER_SKIP = {
    "sk-cron-run": 1,
    "nohup": 0,
    "nice": 0,
    "ionice": 0,
    "env": 0,
    "timeout": 1,
    "flock": 1,
}
_INTERPRETERS = re.compile(r"^(ba|da|z|k)?sh$|^python[0-9.]*$|^perl$|^ruby$|^node$")
_SEPARATORS = ("&&", "||", ";", "|")


@dataclass
class CronFinding:
    """One crontab entry that references something missing."""

    line_no: int
    target: str
    reason: str
    line: str


@dataclass
class CronAudit:
    """Result of auditing a crontab."""

    entries: int = 0
    findings: list[CronFinding] = field(default_factory=list)
    unresolved: int = 0


def read_crontab() -> tuple[Optional[str], str]:
    """Return ``(text, note)`` from ``crontab -l``.

    ``text`` is ``""`` for a user with no crontab, and ``None`` when the
    crontab binary is unavailable or fails (``note`` says why). Read-only.
    """
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"crontab unavailable: {exc}"
    if proc.returncode == 0:
        return proc.stdout, ""
    if "no crontab" in (proc.stderr or "").lower():
        return "", "no crontab for this user"
    return None, (proc.stderr or "crontab -l failed").strip()[:120]


def _split_schedule(line: str) -> Optional[str]:
    """Return the command part of a crontab entry, or None if not an entry."""
    parts = line.split(None, 1)
    if not parts:
        return None
    if parts[0] in _SPECIAL:
        return parts[1] if len(parts) > 1 else None
    parts = line.split(None, 5)
    if len(parts) < 6 or not re.match(r"^[0-9*/,\-]", parts[0]):
        return None
    return parts[5]


def _strip_percent(cmd: str) -> str:
    """Cron treats an unescaped % as end of command (rest goes to stdin)."""
    return re.split(r"(?<!\\)%", cmd, maxsplit=1)[0].replace("\\%", "%")


def _expand(token: str, env: dict[str, str]) -> Optional[str]:
    """Expand ``~`` and ``$VAR``/``${VAR}``; None when a variable is unknown."""
    home = env.get("HOME", str(Path.home()))
    if token == "~" or token.startswith("~/"):
        token = home + token[1:]
    unknown = False

    def sub(m: re.Match) -> str:
        nonlocal unknown
        name = m.group(1) or m.group(2)
        if name in env:
            return env[name]
        unknown = True
        return ""

    out = re.sub(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))", sub, token)
    return None if unknown or "$(" in out or "`" in out else out


def _tokenize(cmd: str) -> list[str]:
    """Shell-split with separators isolated; falls back to whitespace split."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return cmd.split()


def _segments(tokens: list[str]) -> list[list[str]]:
    segs: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _SEPARATORS or set(tok) <= set("&|;"):
            segs.append([])
        else:
            segs[-1].append(tok)
    return [s for s in segs if s]


_REDIRECT_RE = re.compile(r"(?:&>>?|[0-9]*>>?|[0-9]*<)\s*&?\s*[^\s;&|]+")


def _strip_redirects(cmd: str) -> str:
    """Remove ``>file``, ``>>file``, ``2>&1``, ``&>file`` and ``<file`` redirects."""
    return _REDIRECT_RE.sub(" ", cmd)


def _check_path(
    token: str, cwd: Path, env: dict[str, str], path_dirs: list[str]
) -> tuple[str, Optional[str]]:
    """Classify one command token: ``("ok"|"missing"|"unresolved", resolved)``."""
    exp = _expand(token, env)
    if exp is None:
        return "unresolved", None
    if "/" in exp:
        p = Path(exp)
        if not p.is_absolute():
            p = cwd / p
        return ("ok" if p.exists() else "missing"), str(p)
    found = shutil.which(exp, path=os.pathsep.join(path_dirs))
    return ("ok" if found else "missing"), exp


def audit_crontab(text: str, home: Optional[Path] = None) -> CronAudit:
    """Audit crontab *text* and report entries whose targets do not exist."""
    audit = CronAudit()
    home_s = str(home or Path.home())
    env: dict[str, str] = {"HOME": home_s}
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_RE.match(line)
        if m and _split_schedule(line) is None:
            val = m.group(2).strip().strip("\"'")
            env[m.group(1)] = _expand(val, env) or val
            continue
        cmd = _split_schedule(line)
        if cmd is None:
            continue
        audit.entries += 1
        path_dirs = [d for d in env.get("PATH", DEFAULT_CRON_PATH).split(":") if d]
        cwd = Path(home_s)
        cmd = _strip_redirects(_strip_percent(cmd))
        # A trailing comment after the command is not part of it.
        cmd = re.sub(r"\s#\s.*$", "", cmd)
        for seg in _segments(_tokenize(cmd)):
            local = dict(env)
            while seg and _ASSIGN_RE.match(seg[0]):
                k, _, v = seg.pop(0).partition("=")
                local[k] = v
                if k == "PATH":
                    path_dirs = [d for d in v.split(":") if d]
            targets: list[str] = []
            while seg:
                head = seg[0]
                base = os.path.basename(head)
                if head in _BUILTINS:
                    if head == "cd" and len(seg) > 1:
                        d = _expand(seg[1], local)
                        if d is not None:
                            cwd = Path(d) if os.path.isabs(d) else cwd / d
                            if not cwd.is_dir():
                                targets.append(f"cd:{cwd}")
                    seg = []
                    break
                targets.append(head)
                if base in _WRAPPER_SKIP:
                    seg = seg[1:]
                    n = _WRAPPER_SKIP[base]
                    # drop wrapper options, then the fixed leading args
                    while seg and seg[0].startswith("-"):
                        seg = seg[1:]
                    while seg and _ASSIGN_RE.match(seg[0]):
                        seg = seg[1:]
                    seg = seg[n:]
                    continue
                if _INTERPRETERS.match(base):
                    rest = seg[1:]
                    if "-c" in rest or "-m" in rest or "-e" in rest:
                        rest = []
                    rest = [t for t in rest if not t.startswith("-")]
                    if rest:
                        targets.append(rest[0])
                seg = []
            for tgt in targets:
                if tgt.startswith("cd:"):
                    audit.findings.append(CronFinding(no, tgt[3:], "cd directory missing", line))
                    continue
                status, resolved = _check_path(tgt, cwd, local, path_dirs)
                if status == "missing":
                    reason = "not found on PATH" if "/" not in tgt else "path does not exist"
                    audit.findings.append(CronFinding(no, resolved or tgt, reason, line))
                elif status == "unresolved":
                    audit.unresolved += 1
    return audit
