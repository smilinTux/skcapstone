"""
Config file validator for SKCapstone.

Validates consciousness.yaml, model_profiles.yaml, identity.json,
and soul blueprints. Reports errors with file paths and line numbers.

Usage:
    from skcapstone.config_validator import validate_all
    report = validate_all(home_path)
    if not report.is_valid:
        for r in report.results:
            for issue in r.errors:
                print(issue)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


@dataclass
class ValidationIssue:
    """A single validation problem in a config file."""

    severity: str  # "error" | "warning"
    message: str
    field: Optional[str] = None
    line: Optional[int] = None

    def __str__(self) -> str:
        loc = f"line {self.line}" if self.line else ""
        field_str = f" [{self.field}]" if self.field else ""
        suffix = f" ({loc})" if loc else ""
        return f"{self.severity.upper()}{field_str}{suffix}: {self.message}"


@dataclass
class FileValidationResult:
    """Validation outcome for a single config file."""

    config_name: str
    file_path: Path
    found: bool = True
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def is_valid(self) -> bool:
        """True when there are no errors.

        A missing file is considered valid — it simply falls back to
        built-in defaults and produces a warning, not an error.
        """
        return len(self.errors) == 0


@dataclass
class ConfigValidationReport:
    """Aggregated validation report across all config files."""

    results: list[FileValidationResult] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return all(r.is_valid for r in self.results)

    @property
    def total_errors(self) -> int:
        return sum(len(r.errors) for r in self.results)

    @property
    def total_warnings(self) -> int:
        return sum(len(r.warnings) for r in self.results)


# ---------------------------------------------------------------------------
# YAML AST helpers for line-number extraction
# ---------------------------------------------------------------------------


def _yaml_key_line(text: str, key: str) -> Optional[int]:
    """Return the 1-based line number of a top-level YAML key.

    Args:
        text: Raw YAML text.
        key:  Key name to locate.

    Returns:
        Line number (1-based) or None if not found / parse fails.
    """
    try:
        node = yaml.compose(text)
        if not isinstance(node, yaml.MappingNode):
            return None
        for key_node, _ in node.value:
            if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
                return key_node.start_mark.line + 1
    except Exception:
        pass
    return None


def _yaml_seq_item_line(text: str, seq_key: str, idx: int, subkey: str) -> Optional[int]:
    """Return the line of *subkey* inside a sequence-item mapping.

    Navigates ``{seq_key: [{subkey: …}, …]}`` and returns the line of
    *subkey* in the *idx*-th item.  Falls back to the item's opening line
    if *subkey* is absent.

    Args:
        text:    Raw YAML text.
        seq_key: Top-level sequence key (e.g. ``"profiles"``).
        idx:     Zero-based index into the sequence.
        subkey:  Key to locate inside the item mapping.

    Returns:
        Line number (1-based) or None.
    """
    try:
        root = yaml.compose(text)
        if not isinstance(root, yaml.MappingNode):
            return None
        for k, v in root.value:
            if k.value == seq_key and isinstance(v, yaml.SequenceNode):
                if idx < len(v.value):
                    item = v.value[idx]
                    if isinstance(item, yaml.MappingNode):
                        for kk, _ in item.value:
                            if kk.value == subkey:
                                return kk.start_mark.line + 1
                        return item.start_mark.line + 1
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Allowed value sets
# ---------------------------------------------------------------------------

_VALID_FALLBACK_BACKENDS = frozenset([
    "ollama", "grok", "kimi", "nvidia", "anthropic",
    "openai", "passthrough", "groq", "mistral", "perplexity",
])
_VALID_SYSTEM_PROMPT_MODES = frozenset(["standard", "separate_param", "omit"])
_VALID_STRUCTURE_FORMATS = frozenset(["xml", "markdown", "plain"])
_VALID_THINKING_MODES = frozenset(["none", "budget", "toggle", "auto"])
_VALID_TOOL_FORMATS = frozenset(["openai", "anthropic", "mistral"])

_CONSCIOUSNESS_KNOWN_KEYS = frozenset([
    "enabled", "use_inotify", "inotify_debounce_ms", "response_timeout",
    "max_context_tokens", "max_history_messages", "auto_memory", "auto_ack",
    "privacy_default", "max_concurrent_requests", "fallback_chain",
    "desktop_notifications",
])

_IDENTITY_FINGERPRINT_RE = re.compile(r"^[0-9A-Fa-f]{40}$")


# ---------------------------------------------------------------------------
# consciousness.yaml validator
# ---------------------------------------------------------------------------


def validate_consciousness_yaml(path: Path) -> FileValidationResult:
    """Validate consciousness.yaml against the ConsciousnessConfig schema.

    Checks types, value ranges, fallback chain entries, and unknown keys.

    Args:
        path: Path to the consciousness.yaml file.

    Returns:
        FileValidationResult with any errors/warnings found.
    """
    result = FileValidationResult(config_name="consciousness.yaml", file_path=path)

    if not path.exists():
        result.found = False
        result.issues.append(ValidationIssue(
            severity="warning",
            message="File not found — defaults will be used",
        ))
        return result

    text = path.read_text(encoding="utf-8")

    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        line = None
        if hasattr(exc, "problem_mark") and exc.problem_mark:
            line = exc.problem_mark.line + 1
        result.issues.append(ValidationIssue(
            severity="error", message=f"YAML parse error: {exc}", line=line,
        ))
        return result

    if not raw:
        result.issues.append(ValidationIssue(
            severity="warning", message="Empty file — defaults will be used",
        ))
        return result

    if not isinstance(raw, dict):
        result.issues.append(ValidationIssue(
            severity="error",
            message="Top-level value must be a YAML mapping",
            line=1,
        ))
        return result

    # ── Bool fields ──────────────────────────────────────────────────────
    for bf in ("enabled", "use_inotify", "auto_memory", "auto_ack",
               "privacy_default", "desktop_notifications"):
        if bf in raw and not isinstance(raw[bf], bool):
            result.issues.append(ValidationIssue(
                severity="error", field=bf,
                line=_yaml_key_line(text, bf),
                message=f"Expected bool, got {type(raw[bf]).__name__}",
            ))

    # ── Positive-int fields ──────────────────────────────────────────────
    _pos_int: dict[str, int] = {
        "inotify_debounce_ms": 0,
        "response_timeout": 1,
        "max_context_tokens": 1,
        "max_history_messages": 0,
        "max_concurrent_requests": 1,
    }
    for fi, min_val in _pos_int.items():
        if fi not in raw:
            continue
        v = raw[fi]
        line = _yaml_key_line(text, fi)
        if not isinstance(v, int):
            result.issues.append(ValidationIssue(
                severity="error", field=fi, line=line,
                message=f"Expected int, got {type(v).__name__}",
            ))
        elif v <= min_val:
            result.issues.append(ValidationIssue(
                severity="error", field=fi, line=line,
                message=f"Must be > {min_val}, got {v}",
            ))

    # ── fallback_chain ───────────────────────────────────────────────────
    if "fallback_chain" in raw:
        chain = raw["fallback_chain"]
        line = _yaml_key_line(text, "fallback_chain")
        if not isinstance(chain, list):
            result.issues.append(ValidationIssue(
                severity="error", field="fallback_chain", line=line,
                message=f"Expected list, got {type(chain).__name__}",
            ))
        else:
            for i, item in enumerate(chain):
                if not isinstance(item, str):
                    result.issues.append(ValidationIssue(
                        severity="error",
                        field=f"fallback_chain[{i}]",
                        message=f"Expected string, got {type(item).__name__}",
                    ))
                elif item not in _VALID_FALLBACK_BACKENDS:
                    result.issues.append(ValidationIssue(
                        severity="warning",
                        field=f"fallback_chain[{i}]",
                        message=f"Unknown backend '{item}' (may be a custom provider)",
                    ))

    # ── Unknown keys → warnings ──────────────────────────────────────────
    for key in raw:
        if key not in _CONSCIOUSNESS_KNOWN_KEYS:
            result.issues.append(ValidationIssue(
                severity="warning", field=key,
                line=_yaml_key_line(text, key),
                message=f"Unknown config key '{key}'",
            ))

    return result


# ---------------------------------------------------------------------------
# model_profiles.yaml validator
# ---------------------------------------------------------------------------


def validate_model_profiles_yaml(path: Path) -> FileValidationResult:
    """Validate model_profiles.yaml profile list and field types.

    Args:
        path: Path to model_profiles.yaml.

    Returns:
        FileValidationResult with any errors/warnings found.
    """
    result = FileValidationResult(config_name="model_profiles.yaml", file_path=path)

    if not path.exists():
        result.found = False
        result.issues.append(ValidationIssue(
            severity="warning",
            message="File not found — bundled defaults will be used",
        ))
        return result

    text = path.read_text(encoding="utf-8")

    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        line = None
        if hasattr(exc, "problem_mark") and exc.problem_mark:
            line = exc.problem_mark.line + 1
        result.issues.append(ValidationIssue(
            severity="error", message=f"YAML parse error: {exc}", line=line,
        ))
        return result

    if not isinstance(raw, dict):
        result.issues.append(ValidationIssue(
            severity="error",
            message="Top-level value must be a YAML mapping",
            line=1,
        ))
        return result

    if "profiles" not in raw:
        result.issues.append(ValidationIssue(
            severity="error", field="profiles",
            message="Required top-level key 'profiles' is missing",
            line=1,
        ))
        return result

    profiles = raw["profiles"]
    profiles_line = _yaml_key_line(text, "profiles")

    if not isinstance(profiles, list):
        result.issues.append(ValidationIssue(
            severity="error", field="profiles", line=profiles_line,
            message=f"'profiles' must be a list, got {type(profiles).__name__}",
        ))
        return result

    if len(profiles) == 0:
        result.issues.append(ValidationIssue(
            severity="warning", field="profiles", line=profiles_line,
            message="'profiles' list is empty",
        ))
        return result

    _enum_checks: list[tuple[str, frozenset[str]]] = [
        ("system_prompt_mode", _VALID_SYSTEM_PROMPT_MODES),
        ("structure_format", _VALID_STRUCTURE_FORMATS),
        ("thinking_mode", _VALID_THINKING_MODES),
        ("tool_format", _VALID_TOOL_FORMATS),
    ]

    for i, profile in enumerate(profiles):
        prefix = f"profiles[{i}]"
        item_line = _yaml_seq_item_line(text, "profiles", i, "model_pattern")

        if not isinstance(profile, dict):
            result.issues.append(ValidationIssue(
                severity="error", field=prefix, line=item_line,
                message=f"Each profile must be a mapping, got {type(profile).__name__}",
            ))
            continue

        # Required: model_pattern + family
        for req in ("model_pattern", "family"):
            if req not in profile:
                result.issues.append(ValidationIssue(
                    severity="error",
                    field=f"{prefix}.{req}",
                    line=item_line,
                    message=f"Required field '{req}' is missing",
                ))
            elif not isinstance(profile[req], str):
                result.issues.append(ValidationIssue(
                    severity="error",
                    field=f"{prefix}.{req}",
                    line=_yaml_seq_item_line(text, "profiles", i, req),
                    message=f"Expected str, got {type(profile[req]).__name__}",
                ))

        # model_pattern must be a valid regex
        pat = profile.get("model_pattern")
        if isinstance(pat, str):
            try:
                re.compile(pat)
            except re.error as exc:
                result.issues.append(ValidationIssue(
                    severity="error",
                    field=f"{prefix}.model_pattern",
                    line=_yaml_seq_item_line(text, "profiles", i, "model_pattern"),
                    message=f"Invalid regex: {exc}",
                ))

        # Enum fields
        for fname, valid_set in _enum_checks:
            if fname in profile:
                val = profile[fname]
                if isinstance(val, str) and val not in valid_set:
                    result.issues.append(ValidationIssue(
                        severity="error",
                        field=f"{prefix}.{fname}",
                        line=_yaml_seq_item_line(text, "profiles", i, fname),
                        message=(
                            f"Invalid value '{val}', "
                            f"expected one of: {sorted(valid_set)}"
                        ),
                    ))

        # Bool fields
        for bf in ("thinking_enabled", "no_few_shot",
                   "no_cot_instructions", "supports_tool_calling"):
            if bf in profile and not isinstance(profile[bf], bool):
                result.issues.append(ValidationIssue(
                    severity="error",
                    field=f"{prefix}.{bf}",
                    line=_yaml_seq_item_line(text, "profiles", i, bf),
                    message=f"Expected bool, got {type(profile[bf]).__name__}",
                ))

    return result


# ---------------------------------------------------------------------------
# identity.json validator
# ---------------------------------------------------------------------------


def validate_identity_json(path: Path) -> FileValidationResult:
    """Validate identity.json required fields and formats.

    Checks that ``name`` and ``fingerprint`` are present and well-formed.

    Args:
        path: Path to identity.json.

    Returns:
        FileValidationResult with any errors/warnings found.
    """
    result = FileValidationResult(config_name="identity.json", file_path=path)

    if not path.exists():
        result.found = False
        result.issues.append(ValidationIssue(
            severity="warning",
            message="File not found — run 'skcapstone init' to create identity",
        ))
        return result

    text = path.read_text(encoding="utf-8")

    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        result.issues.append(ValidationIssue(
            severity="error",
            message=f"JSON parse error: {exc.msg}",
            line=exc.lineno,
        ))
        return result

    if not isinstance(raw, dict):
        result.issues.append(ValidationIssue(
            severity="error",
            message="Top-level value must be a JSON object",
            line=1,
        ))
        return result

    # Required: name
    if "name" not in raw:
        result.issues.append(ValidationIssue(
            severity="error", field="name",
            message="Required field 'name' is missing",
        ))
    elif not isinstance(raw["name"], str) or not raw["name"].strip():
        result.issues.append(ValidationIssue(
            severity="error", field="name",
            message="'name' must be a non-empty string",
        ))

    # Required: fingerprint
    if "fingerprint" not in raw:
        result.issues.append(ValidationIssue(
            severity="error", field="fingerprint",
            message="Required field 'fingerprint' is missing",
        ))
    elif not isinstance(raw["fingerprint"], str):
        result.issues.append(ValidationIssue(
            severity="error", field="fingerprint",
            message=f"Expected str, got {type(raw['fingerprint']).__name__}",
        ))
    elif not _IDENTITY_FINGERPRINT_RE.match(raw["fingerprint"]):
        result.issues.append(ValidationIssue(
            severity="warning", field="fingerprint",
            message=(
                f"Fingerprint '{raw['fingerprint']}' does not look like a "
                "PGP fingerprint (expected 40 hex characters)"
            ),
        ))

    # Optional: email must be str if present
    if "email" in raw and raw["email"] is not None:
        if not isinstance(raw["email"], str):
            result.issues.append(ValidationIssue(
                severity="error", field="email",
                message=f"Expected str, got {type(raw['email']).__name__}",
            ))

    # Optional: capauth_managed must be bool if present
    if "capauth_managed" in raw and not isinstance(raw["capauth_managed"], bool):
        result.issues.append(ValidationIssue(
            severity="error", field="capauth_managed",
            message=f"Expected bool, got {type(raw['capauth_managed']).__name__}",
        ))

    return result


# ---------------------------------------------------------------------------
# Soul blueprint JSON validator
# ---------------------------------------------------------------------------


def validate_soul_blueprint_json(path: Path) -> FileValidationResult:
    """Validate a soul blueprint JSON file (base.json or installed/*.json).

    Checks that ``name`` and ``display_name`` are present, and that
    ``core_traits`` / ``emotional_topology`` have the expected types.

    Args:
        path: Path to the soul JSON file.

    Returns:
        FileValidationResult with any errors/warnings found.
    """
    config_name = f"soul/{path.name}"
    result = FileValidationResult(config_name=config_name, file_path=path)

    if not path.exists():
        result.found = False
        result.issues.append(ValidationIssue(
            severity="warning", message="File not found",
        ))
        return result

    text = path.read_text(encoding="utf-8")

    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        result.issues.append(ValidationIssue(
            severity="error",
            message=f"JSON parse error: {exc.msg}",
            line=exc.lineno,
        ))
        return result

    if not isinstance(raw, dict):
        result.issues.append(ValidationIssue(
            severity="error", message="Expected a JSON object", line=1,
        ))
        return result

    # Required: name + display_name
    for req in ("name", "display_name"):
        if req not in raw:
            result.issues.append(ValidationIssue(
                severity="error", field=req,
                message=f"Required field '{req}' is missing",
            ))
        elif not isinstance(raw[req], str) or not raw[req].strip():
            result.issues.append(ValidationIssue(
                severity="error", field=req,
                message=f"'{req}' must be a non-empty string",
            ))

    # core_traits: list
    if "core_traits" in raw and not isinstance(raw["core_traits"], list):
        result.issues.append(ValidationIssue(
            severity="error", field="core_traits",
            message=f"Expected list, got {type(raw['core_traits']).__name__}",
        ))

    # emotional_topology: dict[str, float|int]
    if "emotional_topology" in raw:
        et = raw["emotional_topology"]
        if not isinstance(et, dict):
            result.issues.append(ValidationIssue(
                severity="error", field="emotional_topology",
                message=f"Expected dict, got {type(et).__name__}",
            ))
        else:
            for k, v in et.items():
                if not isinstance(v, (int, float)):
                    result.issues.append(ValidationIssue(
                        severity="error",
                        field=f"emotional_topology.{k}",
                        message=f"Expected numeric value, got {type(v).__name__}",
                    ))

    return result


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def validate_all(home: Path) -> ConfigValidationReport:
    """Validate all config files in an agent home directory.

    Checks:
        - ``{home}/config/consciousness.yaml``
        - ``{home}/config/model_profiles.yaml``
        - ``{home}/identity/identity.json``
        - ``{home}/soul/base.json``
        - ``{home}/soul/installed/*.json``

    Files that do not exist are reported as warnings (not errors), since
    missing files fall back to built-in defaults.

    Args:
        home: Agent home directory (e.g. ``~/.skcapstone``).

    Returns:
        ConfigValidationReport aggregating all results.
    """
    report = ConfigValidationReport()
    config_dir = home / "config"

    report.results.append(
        validate_consciousness_yaml(config_dir / "consciousness.yaml")
    )
    report.results.append(
        validate_model_profiles_yaml(config_dir / "model_profiles.yaml")
    )
    report.results.append(
        validate_identity_json(home / "identity" / "identity.json")
    )

    soul_dir = home / "soul"
    if soul_dir.exists():
        report.results.append(validate_soul_blueprint_json(soul_dir / "base.json"))
        installed_dir = soul_dir / "installed"
        if installed_dir.exists():
            for bp_file in sorted(installed_dir.glob("*.json")):
                report.results.append(validate_soul_blueprint_json(bp_file))

    return report
