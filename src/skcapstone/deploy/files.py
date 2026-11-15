"""Files a config instance manages on the host, from ``<instance>/files.yaml``.

A config repository instance (see :mod:`.tools`) can own files outside
systemd, listed in ``<instance>/files.yaml``::

    files:
      - path: ~/.hermes/.env           # a marked block; every other line stays the owner's
        mode: block
        source: hermes.env
      - path: ~/.config/skcall/join.env
        mode: whole                    # the whole file is the source
        source: join.env
      - path: ~/.hermes/plugins/nctalk
        mode: link                     # a symlink into a tool's export
        target: "@TOOL:nctalk@/plugin"

``block`` keeps the file and owns only the lines between two exact markers
(``# >>> sk-deploy (managed by ...) >>>`` and ``# <<< sk-deploy <<<``),
appended at the end the first time. A key the block sets that is also set
outside it (``env``: ``KEY=``/``export KEY=``; ``yaml``: a top-level key) is
refused with its line number, since whichever line wins would be an accident.
``link`` over a real directory moves that directory aside under the deploy
state directory. Everything is rendered and checked before anything is
written, every write is atomic, and :class:`FilesUndo` puts back the previous
text, file mode, symlink or moved directory. No substitution happens beyond
``@TOOL:<name>@`` in link targets: no secret ever goes through here.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, field_validator, model_validator

from .tools import ToolError, default_root, expand_tool_refs

FILES_YAML = "files.yaml"
_KEYS = {
    # python-dotenv also reads ``KEY = v`` and ``'KEY'=v`` as the key KEY.
    "env": re.compile(r"^\s*(?:export\s+)?['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*="),
    "yaml": re.compile(r"^([A-Za-z_][\w-]*):"),
}


class FilesError(ToolError):
    """A managed file cannot be rendered or written; nothing was written."""


def begin_marker(block: str) -> str:
    return f"# >>> {block} (managed by skcapstone deploy; edits here are overwritten) >>>"


def end_marker(block: str) -> str:
    return f"# <<< {block} <<<"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def block_body(text: str, block: str) -> Optional[str]:
    """The lines between *block*'s markers in *text*, or None when it has no such block."""
    parts = text.split("\n")
    begin, end = begin_marker(block), end_marker(block)
    starts = [i for i, line in enumerate(parts) if line.rstrip() == begin]
    ends = [i for i, line in enumerate(parts) if line.rstrip() == end]
    if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
        return None
    inner = parts[starts[0] + 1 : ends[0]]
    return "\n".join(inner) + ("\n" if inner else "")


class FileSpec(BaseModel):
    """One file a config instance manages."""

    path: str
    mode: Literal["whole", "block", "link"]
    source: Optional[str] = None
    target: Optional[str] = None
    format: Literal["env", "yaml", "text"] = "env"
    block: str = "sk-deploy"
    file_mode: int = 0o600

    @field_validator("file_mode", mode="before")
    @classmethod
    def _octal(cls, v):
        if isinstance(v, str):
            v = int(v.removeprefix("0o"), 8)
        if not isinstance(v, int) or not 0 <= v <= 0o777:
            raise ValueError("file_mode is an octal permission, e.g. 0o600")
        return v

    @model_validator(mode="after")
    def _shape(self) -> "FileSpec":
        if not (self.path.startswith("/") or self.path == "~" or self.path.startswith("~/")):
            raise ValueError(f"{self.path}: path is absolute or starts with ~/")
        if self.mode == "link":
            if self.source is not None or not self.target:
                raise ValueError(f"{self.path}: a link takes a target and no source")
            if not self.target.startswith(("/", "~/", "@TOOL:")):
                raise ValueError(f"{self.path}: a link target is absolute, ~/ or @TOOL:<name>@")
        else:
            if self.target is not None or not self.source:
                raise ValueError(f"{self.path}: {self.mode} takes a source and no target")
            parts = Path(self.source).parts
            if self.source.startswith("/") or ".." in parts:
                raise ValueError(f"{self.path}: source is inside the instance (no '..')")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.block):
            raise ValueError(f"{self.path}: block name {self.block!r} is not a plain name")
        return self


def load_files(instance_dir: Path) -> list[FileSpec]:
    """The specs in ``<instance_dir>/files.yaml``; ``[]`` when there is none."""
    f = Path(instance_dir) / FILES_YAML
    if not f.is_file():
        return []
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        specs = [FileSpec.model_validate(s) for s in data.get("files") or []]
    except (yaml.YAMLError, ValueError, AttributeError) as exc:
        raise FilesError(f"{f}: {exc}") from exc
    seen: set[str] = set()
    for s in specs:
        if s.path in seen:
            raise FilesError(f"{f}: {s.path} is listed twice")
        seen.add(s.path)
    return specs


def expand_path(path: str, home: Path) -> Path:
    return (
        Path(home) / path[2:] if path.startswith("~/") else (home if path == "~" else Path(path))
    )


@dataclass
class Rendered:
    """A spec resolved against the host: what *path* should become."""

    spec: FileSpec
    path: Path
    text: Optional[str] = None  # whole/block: the full new content
    link: Optional[str] = None  # link: the symlink target


class _Anything:
    def __contains__(self, _name) -> bool:
        return True


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="surrogateescape")


def _encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


def _keys(lines: list[tuple[int, str]], fmt: str) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    rx = _KEYS.get(fmt)
    if rx is None:
        return out
    for num, line in lines:
        m = rx.match(line)
        if m:
            out.setdefault(m.group(1), []).append(num)
    return out


def _render_block(spec: FileSpec, where: str, body: str, old: str) -> str:
    begin, end = begin_marker(spec.block), end_marker(spec.block)
    if body and not body.endswith("\n"):
        body += "\n"
    if any(line.rstrip() in (begin, end) for line in body.split("\n")):
        raise FilesError(f"{where}: the source {spec.source} contains a block marker")
    parts = old.split("\n")
    starts = [i for i, line in enumerate(parts) if line.rstrip() == begin]
    ends = [i for i, line in enumerate(parts) if line.rstrip() == end]
    if len(starts) > 1 or len(ends) > 1 or len(starts) != len(ends) or starts > ends:
        raise FilesError(
            f"{where}: the {spec.block} block markers are broken (begin marker {len(starts)}"
            f" times, end marker {len(ends)} times); fix the file by hand"
        )
    outside = [
        (i + 1, line) for i, line in enumerate(parts) if not (starts and starts[0] <= i <= ends[0])
    ]
    mine = _keys(outside, spec.format)
    dup = [
        f"{key} on line {', '.join(map(str, mine[key]))}"
        for key in _keys(list(enumerate(body.split("\n"), 1)), spec.format)
        if key in mine
    ]
    if dup:
        raise FilesError(
            f"{where}: the {spec.block} block sets keys also set outside it: {'; '.join(dup)}."
            " Remove those lines (the block owns them)"
        )
    block = f"{begin}\n{body}{end}\n"
    if not starts:
        return old + ("\n" if old and not old.endswith("\n") else "") + block
    b, e = starts[0], ends[0]
    head = "\n".join(parts[:b]) + ("\n" if b else "")
    return head + block + "\n".join(parts[e + 1 :])


def render_files(
    specs: list[FileSpec],
    instance_dir: Path,
    home: Path,
    *,
    tools_on_node=None,
    tools_root: Optional[Path] = None,
    exports: Optional[dict[str, Path]] = None,
    check_links: bool = True,
) -> list[Rendered]:
    """What every spec should make of the host, checked; writes nothing.

    Args:
        tools_on_node: Tool names ``@TOOL:<name>@`` may name (None: any).
        tools_root: The tools root, absolute (a symlink target is a filesystem path).
        exports: Tool name to its new versioned export: a link target under
            ``<tools_root>/<name>/current`` is checked there (before publish).
        check_links: Refuse a link whose target does not exist.

    Raises:
        FilesError: A source is missing, a block is broken or duplicates a
            key, or a link target does not resolve.
        ToolError: A link names a tool the node does not run.
    """
    home = Path(home)
    root = Path(tools_root or default_root(home))
    known = _Anything() if tools_on_node is None else tools_on_node
    out: list[Rendered] = []
    for spec in specs:
        path = expand_path(spec.path, home)
        where = spec.path
        if spec.mode == "link":
            target = expand_tool_refs(spec.target, known, str(root), where)
            target = str(expand_path(target, home))
            if check_links and not _link_exists(target, root, exports):
                raise FilesError(f"{where}: link target {target} does not exist")
            out.append(Rendered(spec, path, link=target))
            continue
        src = Path(instance_dir) / spec.source
        if not src.is_file():
            raise FilesError(f"{where}: source {spec.source} is not in the instance")
        body = _decode(src.read_bytes())
        real = path.resolve() if path.is_symlink() else path
        if real.is_dir():
            raise FilesError(f"{where}: is a directory")
        dup = [
            k
            for k, n in _keys(list(enumerate(body.split("\n"), 1)), spec.format).items()
            if len(n) > 1
        ]
        if dup:
            raise FilesError(f"{where}: {spec.source} sets {', '.join(dup)} more than once")
        if spec.mode == "whole":
            out.append(Rendered(spec, real, text=body))
        else:
            old = _decode(real.read_bytes()) if real.is_file() else ""
            out.append(Rendered(spec, real, text=_render_block(spec, where, body, old)))
    return out


def _link_exists(target: str, root: Path, exports: Optional[dict[str, Path]]) -> bool:
    m = re.match(re.escape(str(root)) + r"/([^/]+)/current(?:/(.*))?$", target)
    if m and exports and m.group(1) in exports:
        return (exports[m.group(1)] / (m.group(2) or "")).exists()
    return Path(target).exists()


@dataclass
class FilesUndo:
    """What :func:`apply_files` replaced, and how to put it back."""

    #: path -> its previous text (None: it was not a regular file)
    previous: dict[str, Optional[str]] = field(default_factory=dict)
    #: link path -> the real path moved aside to make room for it
    displaced: dict[str, str] = field(default_factory=dict)
    #: path -> ("absent",) | ("file", bytes, mode) | ("link", target) | ("moved", aside)
    _was: dict[str, tuple] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self._was

    def entries(self) -> list[tuple[str, tuple]]:
        """``(path, how it was)`` for every path this undo restores, in write order."""
        return list(self._was.items())

    def to_dict(self) -> dict:
        """Plain JSON data (previous bytes as base64), for the change record."""
        out = []
        for key, was in self._was.items():
            entry: dict = {"path": key, "was": was[0]}
            if was[0] == "file":
                entry["data"] = base64.b64encode(was[1]).decode("ascii")
                entry["mode"] = was[2]
            elif was[0] == "link":
                entry["target"] = was[1]
            elif was[0] == "moved":
                entry["aside"] = was[1]
            out.append(entry)
        return {"entries": out}

    @classmethod
    def from_dict(cls, data: dict) -> "FilesUndo":
        """The undo :meth:`to_dict` wrote, in a fresh process."""
        undo = cls()
        for entry in (data or {}).get("entries") or []:
            key, kind = entry["path"], entry["was"]
            undo.previous[key] = None
            if kind == "file":
                raw = base64.b64decode(entry["data"])
                undo._was[key] = ("file", raw, int(entry["mode"]))
                undo.previous[key] = _decode(raw)
            elif kind == "link":
                undo._was[key] = ("link", entry["target"])
            elif kind == "moved":
                undo._was[key] = ("moved", entry["aside"])
                undo.displaced[key] = entry["aside"]
            elif kind == "absent":
                undo._was[key] = ("absent",)
            else:
                raise ValueError(f"{key}: unknown undo kind {kind!r}")
        return undo

    def _note(self, path: Path) -> None:
        key = str(path)
        if key in self._was:
            return
        if path.is_symlink():
            self._was[key] = ("link", os.readlink(path))
            self.previous[key] = None
        elif path.is_file():
            data = path.read_bytes()
            self._was[key] = ("file", data, path.stat().st_mode & 0o777)
            self.previous[key] = _decode(data)
        else:
            self._was[key] = ("absent",)
            self.previous[key] = None

    def restore(self) -> None:
        """Put every path back as it was, last change first.

        Each step is idempotent, so a restore that stopped partway can run again:
        a file already at its previous bytes and mode, a link already at its
        previous target, and a moved directory already back (its aside gone, a
        real directory at the path) are left alone.

        Raises:
            FilesError: A moved directory cannot go back: a real directory is
                already at its path, or its aside is gone. Nothing of that entry
                was changed.
        """
        for key, was in reversed(list(self._was.items())):
            path = Path(key)
            if restored(path, was):
                continue
            if was[0] == "moved":
                problem = moved_back_blocker(path, was[1])
                if problem:
                    raise FilesError(problem)
            if was[0] in ("absent", "moved") and (path.is_symlink() or path.is_file()):
                path.unlink()
            if was[0] == "file":
                _atomic_write(path, was[1], was[2])
            elif was[0] == "link":
                _atomic_symlink(path, was[1])
            elif was[0] == "moved":
                shutil.move(was[1], key)
        self._was.clear()


def _real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def restored(path: Path, was: tuple) -> bool:
    """Whether *path* already is what the undo entry *was* restores."""
    kind = was[0]
    if kind == "absent":
        return not (path.is_symlink() or path.is_file())
    if kind == "link":
        return path.is_symlink() and os.readlink(path) == was[1]
    if kind == "file":
        return (
            not path.is_symlink()
            and path.is_file()
            and path.read_bytes() == was[1]
            and path.stat().st_mode & 0o777 == was[2]
        )
    if kind == "moved":
        aside = Path(was[1])
        return not (aside.exists() or aside.is_symlink()) and _real_dir(path)
    return False


def moved_back_blocker(path: Path, aside: str) -> Optional[str]:
    """Why the directory moved aside to *aside* cannot go back to *path* (None: it can)."""
    a = Path(aside)
    if not (a.exists() or a.is_symlink()):
        return f"{path}: the directory moved aside to {aside} is gone"
    if path.exists() and not (path.is_symlink() or path.is_file()):
        return (
            f"{path} is a real directory now; moving {aside} back would nest it inside."
            " Move it away first"
        )
    return None


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.sk-deploy.{os.getpid()}")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _atomic_symlink(path: Path, target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.sk-deploy.{os.getpid()}")
    tmp.unlink(missing_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, path)


def _aside(path: Path, home: Path, state_dir: Path, stamp: str) -> Path:
    text = str(path)
    rel = text[len(str(home)) + 1 :] if text.startswith(str(home) + "/") else text.lstrip("/")
    dest = Path(state_dir) / "displaced-files" / stamp / rel
    n = 1
    while dest.exists() or dest.is_symlink():
        dest = dest.with_name(f"{Path(rel).name}.{n}")
        n += 1
    return dest


def apply_files(
    specs: list[FileSpec],
    instance_dir: Path,
    home: Path,
    state_dir: Path,
    *,
    tools_on_node=None,
    tools_root: Optional[Path] = None,
    exports: Optional[dict[str, Path]] = None,
    undo: Optional[FilesUndo] = None,
    written: Optional[dict[str, dict]] = None,
) -> dict[str, Optional[str]]:
    """Make the host match *specs*. Everything is checked before anything is written.

    Args:
        written: Filled with what every spec leaves on disk, keyed by path, changed
            or not: ``{"mode", "sha256"}`` of the whole file (plus ``block`` and
            ``block_sha256`` of the block body), or ``{"mode": "link", "target"}``.

    Returns:
        Each path written, to its previous text (None: it was not a regular file).

    Raises:
        FilesError: See :func:`render_files`; nothing was written.
    """
    plan = render_files(
        specs,
        instance_dir,
        home,
        tools_on_node=tools_on_node,
        tools_root=tools_root,
        exports=exports,
    )
    undo = undo if undo is not None else FilesUndo()
    changed: dict[str, Optional[str]] = {}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for r in plan:
        path, key = r.path, str(r.path)
        if written is not None:
            written[key] = _written(r)
        if r.link is not None:
            if path.is_symlink() and os.readlink(path) == r.link:
                continue
            undo._note(path)
            changed[key] = undo.previous[key]
            if path.exists() and not path.is_symlink():
                dest = _aside(path, Path(home), state_dir, stamp)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(key, dest)
                undo._was[key] = ("moved", str(dest))
                undo.displaced[key] = str(dest)
            _atomic_symlink(path, r.link)
            continue
        data = _encode(r.text)
        have = path.read_bytes() if path.is_file() else None
        if have == data and path.stat().st_mode & 0o777 == r.spec.file_mode:
            continue
        undo._note(path)
        changed[key] = undo.previous[key]
        _atomic_write(path, data, r.spec.file_mode)
    return changed


def _written(r: Rendered) -> dict:
    if r.link is not None:
        return {"mode": "link", "target": r.link}
    out = {"mode": r.spec.mode, "sha256": sha256(_encode(r.text))}
    if r.spec.mode == "block":
        body = block_body(r.text, r.spec.block) or ""
        out.update(block=r.spec.block, block_sha256=sha256(_encode(body)))
    return out


def diff_files(
    specs: list[FileSpec],
    instance_dir: Path,
    home: Path,
    *,
    tools_on_node=None,
    tools_root: Optional[Path] = None,
) -> list[str]:
    """Unified diffs of what :func:`apply_files` would change (links as ``-> target``)."""
    out: list[str] = []
    plan = render_files(
        specs,
        instance_dir,
        home,
        tools_on_node=tools_on_node,
        tools_root=tools_root,
        check_links=False,
    )
    for r in plan:
        if r.link is not None:
            if r.path.is_symlink():
                old = [f"-> {os.readlink(r.path)}\n"]
            else:
                old = ["(a directory)\n"] if r.path.is_dir() else []
            new = [f"-> {r.link}\n"]
        else:
            old = _decode(r.path.read_bytes()).splitlines(True) if r.path.is_file() else []
            new = r.text.splitlines(True)
        if old != new:
            out.append("".join(difflib.unified_diff(old, new, str(r.path), str(r.path))))
    return out
