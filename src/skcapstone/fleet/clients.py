"""skfleet-clients: pinned pi/opencode/codex/claude with gateway-generated configs.

Design: docs/fleet/CLIENTS.md. Data lives in skgateway-config-chi ``clients/``:

- ``pins.yaml``          versions, install sources, ring, canary
- ``templates/*.yaml``   the managed part of each client config
- ``hosts/<host>.yaml``  optional per-host overrides

This module is pure: it loads the spec, reads the gateway catalog and renders
the per-host desired state. Applying it on a host is the job of the stdlib-only
agent (``data/skfleet-clients-agent.py``) that runs there over ssh, so secrets
never leave the host: templates carry ``$secret:<name>`` placeholders that the
agent fills from the host's own live configs.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

CLIENT_NAMES = ("pi", "opencode", "codex", "claude")
SECRET_PREFIX = "$secret:"
CATALOG_MAX_BYTES = 4 * 1024 * 1024


class SpecError(ValueError):
    """The clients spec in the config repo is invalid."""


@dataclass(frozen=True)
class ClientPin:
    name: str
    version: str
    source: str  # npm | release
    entry: str  # path of the executable inside the version dir
    package: str = ""
    url: str = ""
    sha256: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    hold: bool = False  # bumps need a human merge
    gateway_env: Mapping[str, str] = field(default_factory=dict)
    upstream: Mapping[str, str] = field(default_factory=dict)
    smoke: tuple[str, ...] = ()
    smoke_expect: str = ""

    def resolved_url(self) -> str:
        return self.url.replace("{version}", self.version)

    def as_install(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "package": self.package,
            "url": self.resolved_url(),
            "sha256": self.sha256,
            "entry": self.entry,
            "env": dict(self.env),
        }


@dataclass(frozen=True)
class Spec:
    root: Path
    install_root: str
    bin_dir: str
    gateway: str
    canary: str
    ring: tuple[str, ...]
    notify: tuple[str, ...]
    clients: Mapping[str, ClientPin]
    templates: Mapping[str, Mapping[str, Any]]
    hosts: Mapping[str, Mapping[str, Any]]
    max_model_drop: float

    def host_clients(self, host: str) -> list[str]:
        skip = set(self.hosts.get(host, {}).get("skip_clients") or [])
        return [c for c in self.clients if c not in skip]


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise SpecError(f"{path}: {exc}") from None
    if not isinstance(data, dict):
        raise SpecError(f"{path}: not a mapping")
    return data


def load_spec(root: Path | str) -> Spec:
    """Load ``clients/`` from a config repo checkout (or the dir itself)."""
    root = Path(root).expanduser()
    if (root / "clients" / "pins.yaml").exists():
        root = root / "clients"
    pins = _load_yaml(root / "pins.yaml")
    if pins.get("schema") != 1:
        raise SpecError("pins.yaml: schema must be 1")
    clients: dict[str, ClientPin] = {}
    for name, raw in (pins.get("clients") or {}).items():
        if name not in CLIENT_NAMES:
            raise SpecError(f"pins.yaml: unknown client {name!r}")
        if not isinstance(raw, dict) or not raw.get("version") or not raw.get("entry"):
            raise SpecError(f"pins.yaml: {name} needs version and entry")
        source = raw.get("source")
        if source == "npm" and not raw.get("package"):
            raise SpecError(f"pins.yaml: {name} npm source needs package")
        if source == "release" and not (
            raw.get("url") and re.fullmatch(r"[0-9a-f]{64}", str(raw.get("sha256", "")))
        ):
            raise SpecError(f"pins.yaml: {name} release source needs url and sha256")
        if source not in ("npm", "release"):
            raise SpecError(f"pins.yaml: {name} source must be npm or release")
        clients[name] = ClientPin(
            name=name,
            version=str(raw["version"]),
            source=source,
            entry=str(raw["entry"]),
            package=str(raw.get("package", "")),
            url=str(raw.get("url", "")),
            sha256=str(raw.get("sha256", "")),
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            hold=bool(raw.get("hold", False)),
            gateway_env=dict(raw.get("gateway_env") or {}),
            upstream=dict(raw.get("upstream") or {}),
            smoke=tuple(str(a) for a in (raw.get("smoke") or ())),
            smoke_expect=str(raw.get("smoke_expect", "")),
        )
    ring = tuple(pins.get("ring") or ())
    canary = str(pins.get("canary") or (ring[0] if ring else ""))
    if not ring or ring[0] != canary:
        raise SpecError("pins.yaml: ring must start with the canary")
    templates: dict[str, Mapping[str, Any]] = {}
    for path in sorted((root / "templates").glob("*.yaml")):
        tpl = _load_yaml(path)
        if tpl.get("client") not in clients:
            raise SpecError(f"{path.name}: client must be one of the pinned clients")
        if tpl.get("format") not in ("json", "toml"):
            raise SpecError(f"{path.name}: format must be json or toml")
        if not tpl.get("file"):
            raise SpecError(f"{path.name}: file is required")
        templates[path.stem] = tpl
    hosts = {p.stem: _load_yaml(p) for p in sorted((root / "hosts").glob("*.yaml"))}
    unknown = set(hosts) - set(ring)
    if unknown:
        raise SpecError(f"hosts/: overrides for hosts not in the ring: {sorted(unknown)}")
    return Spec(
        root=root,
        install_root=str(pins.get("install_root") or "~/.local/opt/sk-clients"),
        bin_dir=str(pins.get("bin_dir") or "~/.local/bin"),
        gateway=str(pins.get("gateway") or "").rstrip("/"),
        canary=canary,
        ring=ring,
        notify=tuple(pins.get("notify") or ()),
        clients=clients,
        templates=templates,
        hosts=hosts,
        max_model_drop=float(pins.get("max_model_drop", 0.25)),
    )


# ---------------------------------------------------------------- catalog


def fetch_catalog(
    gateway: str,
    *,
    timeout: float = 10.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> list[dict[str, Any]]:
    """Return the gateway's ``/v1/models`` rows. Raises on any doubt."""
    with opener(gateway.rstrip("/") + "/v1/models", timeout=timeout) as resp:
        raw = resp.read(CATALOG_MAX_BYTES + 1)
    if len(raw) > CATALOG_MAX_BYTES:
        raise ValueError("gateway catalog exceeds bound")
    doc = json.loads(raw)
    rows = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("gateway catalog has no data rows")
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)]


def _fp_row(row: Mapping[str, Any]) -> dict[str, Any]:
    card = row.get("card") if isinstance(row.get("card"), dict) else {}
    return {
        "id": row.get("id"),
        "owned_by": row.get("owned_by"),
        "advertised": row.get("advertised"),
        "stale": row.get("stale"),
        "vision": row.get("vision"),
        "ctx": card.get("context_length") or row.get("ctx_tokens"),
        "max_out": card.get("max_output_tokens"),
        "name": card.get("display_name"),
    }


def catalog_fingerprint(rows: list[Mapping[str, Any]]) -> str:
    """Hash of the fields that can change a rendered config."""
    items = sorted((_fp_row(r) for r in rows), key=lambda r: str(r["id"]))
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- render


def _present(row: Mapping[str, Any] | None) -> bool:
    """Curated models stay while the gateway lists them and has not retired them."""
    return row is not None and row.get("advertised") is not False and row.get("stale") is not True


def _new_model(row: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    card = row.get("card") if isinstance(row.get("card"), dict) else {}
    model: dict[str, Any] = {"id": row["id"], "name": card.get("display_name") or row["id"]}
    model["reasoning"] = bool(defaults.get("reasoning", True))
    vision = row.get("vision") is True and defaults.get("image", True)
    model["input"] = ["text", "image"] if vision else ["text"]
    ctx = card.get("context_length") or row.get("ctx_tokens")
    if isinstance(ctx, int) and ctx > 0:
        model["contextWindow"] = ctx
    return model


def select_models(
    rule: Mapping[str, Any], rows_by_id: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Curated models still served, in curated order, then new auto-added ones."""
    curated = [m for m in rule.get("models") or [] if isinstance(m, dict) and m.get("id")]
    out = [copy.deepcopy(m) for m in curated if _present(rows_by_id.get(m["id"]))]
    auto = rule.get("auto_add") or {}
    owners = set(auto.get("owners") or [])
    if owners:
        known = {m["id"] for m in curated} | set(auto.get("exclude") or [])
        pattern = re.compile(auto["match"]) if auto.get("match") else None
        for mid in sorted(rows_by_id):
            row = rows_by_id[mid]
            if mid in known or row.get("owned_by") not in owners:
                continue
            if row.get("advertised") is not True or row.get("stale") is True:
                continue
            if pattern and not pattern.search(mid):
                continue
            out.append(_new_model(row, auto.get("defaults") or {}))
    return out


def _subst(value: Any, ctx: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for key, val in ctx.items():
            value = value.replace("{" + key + "}", val)
        return value
    if isinstance(value, dict):
        return {k: _subst(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, ctx) for v in value]
    return value


def render_template(
    tpl: Mapping[str, Any], rows: list[Mapping[str, Any]], *, gateway: str
) -> dict[str, Any]:
    """Render one template into ``{file, format, set, preserve}``.

    ``set`` maps a dotted path to the value that path must hold. JSON paths are
    replaced whole (except ``preserve`` keys, which the agent copies from the
    live file); TOML paths are top-level keys only.
    """
    rows_by_id = {r["id"]: r for r in rows}
    ctx = {"gateway": gateway}
    managed: dict[str, Any] = {}
    for path, value in (tpl.get("set") or {}).items():
        managed[path] = _subst(value, ctx)
    style = tpl.get("models_style", "pi")
    for path, block in (tpl.get("providers") or {}).items():
        block = dict(block)
        rule = {k: block.pop(k) for k in ("models", "auto_add") if k in block}
        rendered = _subst(block, ctx)
        models = select_models(rule, rows_by_id)
        if style == "opencode":
            rendered["models"] = {
                m["id"]: {k: v for k, v in m.items() if k != "id"} for m in models
            }
        else:
            rendered["models"] = models
        managed[path] = rendered
    if tpl["format"] == "toml" and any("." in p for p in managed):
        raise SpecError("toml templates may only set top-level keys")
    return {
        "file": tpl["file"],
        "format": tpl["format"],
        "set": managed,
        "preserve": list(tpl.get("preserve") or []),
    }


def count_models(rendered: Mapping[str, Any]) -> int:
    total = 0
    for value in rendered["set"].values():
        if isinstance(value, dict) and isinstance(value.get("models"), (list, dict)):
            total += len(value["models"])
    return total


def render_host(spec: Spec, host: str, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Desired state for one host: installs, shims and config fragments."""
    wanted = spec.host_clients(host)
    skip_tpl = set(spec.hosts.get(host, {}).get("skip_templates") or [])
    files = [
        render_template(tpl, rows, gateway=spec.gateway)
        for name, tpl in spec.templates.items()
        if tpl["client"] in wanted and name not in skip_tpl
    ]
    pi_models = [
        [path.split(".", 1)[1], m["id"]]
        for f in files
        if f["file"].endswith("/.pi/agent/models.json")
        for path, block in f["set"].items()
        if path.startswith("providers.") and isinstance(block, dict)
        for m in block.get("models") or []
    ]
    return {
        "host": host,
        "install_root": spec.install_root,
        "bin_dir": spec.bin_dir,
        "clients": [spec.clients[c].as_install() for c in wanted],
        "files": files,
        "pi_models": pi_models,
    }


def state_hash(desired: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(desired, sort_keys=True).encode()).hexdigest()


def model_drop(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> float:
    """Fraction of rendered models that vanished since the last rolled render."""
    if not old:
        return 0.0
    before = sum(count_models(f) for f in old.get("files", []))
    after = sum(count_models(f) for f in new.get("files", []))
    if before <= 0:
        return 0.0
    return max(0.0, (before - after) / before)
