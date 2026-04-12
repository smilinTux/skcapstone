#!/usr/bin/env python3
"""
session-to-memory.py — Extract an OpenClaw session jsonl and save a digest to skmemory.

Usage:
  python3 session-to-memory.py <session.jsonl> [--agent lumina] [--dry-run]

Called by archive-sessions.sh before archiving each session file.
Also useful to run manually against any archived session.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SKIP_PREFIXES = (
    "[SKMemory",
    "[System",
    "[skmemory",
    "--- SKMEMORY",
    "--- SKWHISPER",
)

MAX_CONTENT_CHARS = 12000  # ~3k tokens — enough for a solid digest without blowing budget
CLAUDE_MODEL = "claude-haiku-4-5"  # fast + cheap for digest work


def extract_turns(path: Path) -> list[tuple[str, str]]:
    """Parse a session jsonl and return real (role, text) turns, skipping injections."""
    turns = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if obj.get("type") != "message":
                continue

            # Handle both top-level message fields and nested .message
            m = obj.get("message", obj)
            role = m.get("role", "?")
            content = m.get("content", "")

            if isinstance(content, list):
                text = " ".join(
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            else:
                text = str(content)

            text = text.strip()
            if not text or len(text) < 5:
                continue
            if any(text.startswith(p) for p in SKIP_PREFIXES):
                continue

            turns.append((role, text))

    return turns


def turns_to_prompt(turns: list[tuple[str, str]], max_chars: int = MAX_CONTENT_CHARS) -> str:
    """Format turns as a conversation snippet, truncated to max_chars."""
    lines = []
    for role, text in turns:
        prefix = "Chef" if role == "user" else "Lumina"
        lines.append(f"{prefix}: {text[:600]}")
    full = "\n\n".join(lines)
    if len(full) > max_chars:
        full = full[:max_chars] + "\n\n[... truncated ...]"
    return full


def generate_digest(conversation: str, session_id: str) -> str:
    """Use claude CLI to generate a session digest."""
    prompt = f"""You are summarizing an OpenClaw AI agent session for the skmemory system.
Session ID: {session_id}

Conversation:
{conversation}

Write a concise session digest (3-6 sentences) covering:
- Key topics discussed
- Decisions made or actions taken
- Any notable moments or outcomes

Be specific. Use past tense. No preamble."""

    try:
        result = subprocess.run(
            [
                "claude", "--print",
                "--dangerously-skip-permissions",
                "--model", CLAUDE_MODEL,
                "--output-format", "json",
                "--no-session-persistence",
            ],
            input=prompt.encode(),
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            return f"[digest failed: {result.stderr.decode()[:200]}]"
        parsed = json.loads(result.stdout.decode())
        return parsed.get("result", "").strip()
    except Exception as e:
        return f"[digest error: {e}]"


def save_to_skmemory(title: str, content: str, agent: str, tags: list[str]) -> bool:
    """Save a memory snapshot via skmemory CLI."""
    tag_str = ",".join(tags)
    try:
        result = subprocess.run(
            [
                "skmemory", "snapshot",
                title, content,
                "--layer", "mid-term",
                "--tags", tag_str,
            ],
            capture_output=True,
            timeout=30,
            env={**os.environ, "SKAGENT": agent, "SKCAPSTONE_AGENT": agent},
        )
        if result.returncode != 0:
            print(f"  [skmemory error] {result.stderr.decode()[:200]}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"  [skmemory exception] {e}", file=sys.stderr)
        return False


def process_session(path: Path, agent: str = "lumina", dry_run: bool = False) -> bool:
    session_id = path.stem[:8]

    # Infer date from jsonl (first session entry)
    session_date = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("type") == "session":
                    ts = obj.get("timestamp", "")
                    if ts:
                        session_date = ts[:10]
                    break
    except Exception:
        pass

    turns = extract_turns(path)
    if not turns:
        print(f"  No usable turns in {path.name} — skipping.")
        return False

    print(f"  {len(turns)} turns extracted from {path.name}")

    date_str = session_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = f"Session Digest — {date_str} ({session_id})"

    if dry_run:
        conv = turns_to_prompt(turns)
        print(f"  [dry-run] Would save: {title}")
        print(f"  Conversation preview ({len(conv)} chars):")
        print(conv[:400])
        return True

    conversation = turns_to_prompt(turns)
    print(f"  Generating digest via claude ({CLAUDE_MODEL})...")
    digest = generate_digest(conversation, session_id)

    if not digest or digest.startswith("[digest"):
        print(f"  Digest generation failed: {digest}")
        return False

    content = f"**Session:** `{session_id}`  \n**Date:** {date_str}  \n**Turns:** {len(turns)}\n\n{digest}"
    tags = ["auto-digest", "session-archive", f"session:{session_id}", f"agent:{agent}"]

    print(f"  Saving memory: {title}")
    ok = save_to_skmemory(title, content, agent, tags)
    if ok:
        print(f"  Saved to skmemory (mid-term).")
    return ok


def main():
    parser = argparse.ArgumentParser(description="Extract session to skmemory digest")
    parser.add_argument("session_file", help="Path to session .jsonl file")
    parser.add_argument("--agent", default="lumina", help="Agent name (default: lumina)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving")
    args = parser.parse_args()

    path = Path(args.session_file)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    print(f"Processing: {path.name}")
    ok = process_session(path, agent=args.agent, dry_run=args.dry_run)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
