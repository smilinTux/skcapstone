"""A deliberately small, fail-closed subset of Syncthing ignore semantics.

Syncthing reads the ``.stignore`` at each folder root to decide what never
leaves the node. Auditing that decision needs to answer one question per
path: can this path synchronize? This module answers it with an explicit
subset of the real semantics, and answers "uncertain" (which callers must
treat as NOT covered) whenever a rule uses anything outside the subset.

Supported subset:

* comments (``//`` and ``#``) and blank lines
* ``(?d)`` and ``(?i)`` prefixes, in any combination; any other ``(?x)``
  prefix makes the rule unsupported, so every path it might decide is
  reported uncertain
* ``!`` negation is parsed, but any path matched by ANY negation rule is
  reported uncertain: whether a re-inclusion takes effect depends on
  traversal order (Syncthing never descends into an ignored directory), and
  the safe answer for a leak audit is "cannot prove coverage"
* leading ``/`` anchoring to the folder root, and implicit root anchoring
  for any pattern containing an interior slash
* ``**`` as a whole path component (zero or more components), ``*`` and
  ``?`` inside a component, and ``[...]`` character classes
* a trailing ``/`` marks a directory-only rule; matching a directory ignores
  everything beneath it

Anything else (malformed classes, doubled negation, bare prefix lines) is
unsupported and therefore uncertain. Uncertain NEVER reports covered.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class Coverage(enum.Enum):
    """Whether one relative path is kept off the wire by an ignore ruleset."""

    IGNORED = "ignored"
    EXPOSED = "exposed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class CompiledPattern:
    """One parsed .stignore line ready for matching.

    Attributes:
        raw: The original stripped line, kept for reporting.
        regex: Compiled matcher applied to candidate path prefixes.
        negated: True for ``!`` re-inclusion rules.
        directory_only: True for rules ending in ``/``.
        supported: False when the line uses syntax outside the subset.
    """

    raw: str
    regex: re.Pattern[str] | None
    negated: bool
    directory_only: bool
    supported: bool


_PREFIX_RE = re.compile(r"^\(\?([a-z]+)\)")
_CLASS_RE = re.compile(r"\[[^\[\]]*\]")


def _translate_component(component: str) -> str | None:
    """Translate one pattern component to regex source, or None if unsupported.

    Args:
        component: A single path component of a pattern body.

    Returns:
        Regex source for the component, or None when it uses syntax outside
        the supported subset.
    """
    out: list[str] = []
    index = 0
    while index < len(component):
        char = component[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            match = _CLASS_RE.match(component, index)
            if match is None:
                return None
            out.append(match.group(0))
            index = match.end() - 1
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def _translate_body(body: str, anchored: bool) -> str | None:
    """Translate a pattern body to a full-match regex, or None if unsupported.

    ``**`` is honored only as a whole path component, matching zero or more
    components, which is the only meaning Syncthing assigns it.
    """
    if not body:
        return None
    parts = body.split("/")
    segments: list[str] = []
    for index, part in enumerate(parts):
        if part == "**":
            leading = index == 0
            trailing = index == len(parts) - 1
            if leading and trailing:
                segments.append(".*")
            elif leading:
                segments.append("(?:.*/)?")
            elif trailing:
                segments.append("(?:/.*)?")
            else:
                segments.append("(?:.*/)?")
            continue
        if "**" in part:
            return None
        translated = _translate_component(part)
        if translated is None:
            return None
        segments.append(translated)
    joined = ""
    for index, segment in enumerate(segments):
        if index:
            prior = parts[index - 1] == "**"
            current = parts[index] == "**"
            if not prior and not current:
                joined += "/"
            elif prior and not current:
                # The trailing "/" of the "**" expansion already separates.
                pass
            elif not prior and current:
                joined += "/"
        joined += segment
    if anchored:
        return "^" + joined + "$"
    return "^(?:.*/)?" + joined + "$"


def compile_pattern(line: str) -> CompiledPattern | None:
    """Compile one non-comment .stignore line.

    Args:
        line: A single stripped pattern line.

    Returns:
        A CompiledPattern, or None when the line is empty after stripping.
        Lines outside the supported subset compile with ``supported=False``
        and never match, which forces callers into the uncertain branch.
    """
    case_insensitive = False
    rest = line
    while rest.startswith("(?"):
        match = _PREFIX_RE.match(rest)
        if match is None:
            return CompiledPattern(line, None, False, False, False)
        flags = match.group(1)
        if any(flag not in "di" for flag in flags):
            return CompiledPattern(line, None, False, False, False)
        case_insensitive = case_insensitive or "i" in flags
        rest = rest[match.end() :]

    negated = rest.startswith("!")
    if negated:
        rest = rest[1:]
        if rest.startswith("!"):
            return CompiledPattern(line, None, True, False, False)

    directory_only = rest.endswith("/")
    if directory_only:
        rest = rest.rstrip("/")

    anchored = rest.startswith("/")
    if anchored:
        rest = rest.lstrip("/")
    elif "/" in rest:
        anchored = True

    source = _translate_body(rest, anchored)
    if source is None:
        return CompiledPattern(line, None, negated, directory_only, False)
    flags = re.IGNORECASE if case_insensitive else 0
    return CompiledPattern(line, re.compile(source, flags), negated, directory_only, True)


def load_ruleset(text: str) -> list[CompiledPattern]:
    """Parse .stignore contents into ordered compiled patterns.

    Args:
        text: Raw file contents.

    Returns:
        Compiled patterns in file order; comment and blank lines are dropped.
    """
    rules: list[CompiledPattern] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        rule = compile_pattern(line)
        if rule is not None:
            rules.append(rule)
    return rules


def _prefixes(relpath: str) -> list[str]:
    """A path and every ancestor directory path, root-most last.

    Args:
        relpath: Slash-separated path relative to the folder root.

    Returns:
        Candidate prefixes, longest last so deeper rules evaluate first.
    """
    parts = [part for part in relpath.split("/") if part]
    return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]


def evaluate(rules: list[CompiledPattern], relpath: str, *, is_dir: bool = False) -> Coverage:
    """Decide whether a ruleset keeps one relative path off the wire.

    Args:
        rules: Ordered compiled patterns from the folder root's .stignore.
        relpath: Slash-separated path relative to the folder root.
        is_dir: True when the path names a directory.

    Returns:
        Coverage.IGNORED only when the last matching rule is a positive
        match and no unsupported rule or negation clouds the answer.
        Coverage.UNCERTAIN whenever evaluation cannot prove the answer;
        callers fail closed and treat it as uncovered.
    """
    normalized = relpath.strip("/")
    if not normalized:
        return Coverage.EXPOSED
    candidates = _prefixes(normalized)
    if is_dir:
        ancestor_candidates = candidates
    else:
        ancestor_candidates = candidates[:-1]

    matched = False
    ignored = False
    uncertain = False
    for rule in rules:
        if not rule.supported:
            uncertain = True
            continue
        assert rule.regex is not None
        pool = ancestor_candidates if rule.directory_only else candidates
        if any(rule.regex.match(candidate) for candidate in pool):
            matched = True
            if rule.negated:
                uncertain = True
            else:
                ignored = True
            # Later rules decide; negations stay uncertainty markers even
            # when a later positive rule matches, because traversal order
            # decides whether the negation was ever reachable.
    if uncertain:
        return Coverage.UNCERTAIN
    if not matched:
        return Coverage.EXPOSED
    return Coverage.IGNORED if ignored else Coverage.EXPOSED
