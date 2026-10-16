"""Bound and de-noise dispatcher subprocess output before it enters a receipt.

Fleet seat dispatchers (see :mod:`skcapstone.seat_cycle_entrypoint`) run as subprocesses whose raw
stdout and stderr are embedded verbatim into a JSON health receipt once
per cycle. A single noisy line, repeated by something unrelated to the
dispatch itself (for example a CardStore fold warning re-emitted once per
card it folds), can turn into hundreds of copies of the same line. That
is enough to push the receipt past journald's per-message size cap
(48 KiB). A receipt truncated there is no longer valid JSON, so the
operator loses all visibility into why the cycle failed, which is far
worse than the noisy line ever was.

``condense_dispatcher_output`` makes that impossible: it always returns
a value that fits comfortably inside a journald message, and it
collapses repeated lines first so the one line that actually matters
(the real error, not its five hundred repeats) survives instead of being
the thing that gets truncated away.
"""

from __future__ import annotations

#: Bound applied to each of dispatcher stdout and dispatcher stderr
#: independently. journald caps a single log message at 48 KiB (49152
#: bytes). A receipt carries both streams plus other small fields
#: (host, cycle id, activation metadata, mailbox digest), so each
#: stream is bounded well under half the cap to leave comfortable room
#: for the rest of the payload and for JSON escaping overhead.
MAX_DISPATCHER_TEXT_BYTES = 8_192

#: Minimum run length before a repeated line is collapsed. Two in a row
#: is already worth naming; below that there is nothing to collapse.
_MIN_REPEAT_RUN = 2

_ELISION_TEMPLATE = "... [{count} bytes elided] ..."

#: Fixed budget reserved for the elision marker itself when truncating,
#: so the marker text is never squeezed out by the head/tail split.
_ELISION_MARKER_BUDGET = 96


def _to_text(value: object) -> str:
    """Best-effort, never-raising conversion of subprocess output to str."""

    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    try:
        return str(value)
    except Exception:
        return ""


def _collapse_repeated_lines(text: str) -> str:
    """Collapse consecutive runs of the same line into one line plus a count.

    Two lines are treated as the same for this purpose if they are equal
    after stripping trailing whitespace, which absorbs the common
    near-identical case of a repeated line that only differs by a
    trailing space or carriage return. The first occurrence's exact text
    is kept as the representative line.
    """

    if not text:
        return text
    lines = text.split("\n")
    total = len(lines)
    collapsed: list[str] = []
    index = 0
    while index < total:
        line = lines[index]
        key = line.rstrip()
        end = index + 1
        while end < total and lines[end].rstrip() == key:
            end += 1
        run_length = end - index
        if run_length >= _MIN_REPEAT_RUN:
            collapsed.append(f"{line}  [repeated {run_length} times]")
        else:
            collapsed.append(line)
        index = end
    return "\n".join(collapsed)


def _bound_bytes(text: str, max_bytes: int) -> str:
    """Keep head and tail of text, eliding the middle to fit max_bytes.

    The tail is preserved deliberately: for a dispatcher that fails
    partway through, the actual error is usually the last thing it
    printed, and a head-only truncation would throw that away.
    """

    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    remaining = max(max_bytes - _ELISION_MARKER_BUDGET, 0)
    head_budget = remaining // 2
    tail_budget = remaining - head_budget
    head = raw[:head_budget].decode("utf-8", errors="ignore")
    tail = raw[len(raw) - tail_budget :].decode("utf-8", errors="ignore") if tail_budget else ""
    elided = len(raw) - head_budget - tail_budget
    marker = _ELISION_TEMPLATE.format(count=elided)
    return f"{head}\n{marker}\n{tail}"


def condense_dispatcher_output(
    value: object, *, max_bytes: int = MAX_DISPATCHER_TEXT_BYTES
) -> object:
    """Return a bounded, de-duplicated version of subprocess output.

    None is passed through unchanged, so a field that legitimately has
    no dispatcher output stays absent rather than becoming an empty
    string. Anything else (str, bytes, or odd input that is neither) is
    converted to a plain string, has repeated lines collapsed, and is
    then bounded to at most roughly max_bytes bytes of UTF-8 (a small
    fixed amount of headroom is kept for the elision marker). This
    function is deterministic and never raises.
    """

    if value is None:
        return None
    try:
        text = _to_text(value)
        text = _collapse_repeated_lines(text)
        text = _bound_bytes(text, max_bytes)
        return text
    except Exception:
        return ""


#: Default budget for the tail view used when a cycle dies (timeout/crash).
MAX_DISPATCHER_TAIL_BYTES = 16_384


def dedupe_lines(text: str) -> tuple[str, int, int]:
    """Collapse repeated lines ANYWHERE in ``text``, keeping the last occurrence.

    ``_collapse_repeated_lines`` only merges adjacent runs, so a noise line that
    alternates with other lines survives in full and fills the budget. Here every
    distinct line (compared after ``rstrip``) appears once, at the position of its
    last occurrence, suffixed ``  [xN]`` when it occurred N > 1 times, so the final
    state of the stream is preserved. Returns ``(text, total_lines, unique_lines)``.
    """

    lines = text.split("\n")
    trailing = bool(lines) and lines[-1] == ""
    if trailing:
        lines.pop()
    counts: dict[str, int] = {}
    last_index: dict[str, int] = {}
    for index, line in enumerate(lines):
        key = line.rstrip()
        counts[key] = counts.get(key, 0) + 1
        last_index[key] = index
    kept = []
    for index, line in enumerate(lines):
        key = line.rstrip()
        if last_index[key] != index:
            continue
        count = counts[key]
        kept.append(f"{line}  [x{count}]" if count > 1 else line)
    return "\n".join(kept) + ("\n" if trailing and kept else ""), len(lines), len(kept)


def condense_dispatcher_tail(
    value: object, *, max_bytes: int = MAX_DISPATCHER_TAIL_BYTES
) -> object:
    """De-duplicate repeated lines, then keep the LAST ``max_bytes`` bytes.

    Used when a dispatcher cycle is killed: the head of its stderr is startup
    noise, and the cause (the traceback or the last progress lines) is at the
    end. None passes through; never raises.
    """

    if value is None:
        return None
    try:
        text, total, unique = dedupe_lines(_to_text(value))
        raw = text.encode("utf-8", errors="replace")
        if len(raw) > max_bytes:
            keep = max(max_bytes - _ELISION_MARKER_BUDGET, 0)
            tail = raw[len(raw) - keep :].decode("utf-8", errors="ignore") if keep else ""
            tail = tail.partition("\n")[2] if "\n" in tail else tail
            marker = _ELISION_TEMPLATE.format(count=len(raw) - len(tail.encode("utf-8")))
            text = f"{marker}\n{tail}"
        if total != unique:
            text = f"[deduplicated {total} lines to {unique}]\n{text}"
        return text
    except Exception:
        return ""
