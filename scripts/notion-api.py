#!/usr/bin/env python3
"""
notion-api — Simple Notion API wrapper for AI agents.

Lets agents read and update Notion pages via CLI without needing
to construct raw curl/JSON. Uses NOTION_API_KEY from environment.

Usage:
    notion-api read <page-id>
    notion-api append <page-id> <markdown-text>
    notion-api replace <page-id> <markdown-text>
    notion-api add-todo <page-id> <text> [--checked]
    notion-api list-blocks <page-id>

Examples:
    notion-api read 31e2be82-a3a1-8178-820c-e6eeb11b15c1
    notion-api append 31e2be82-a3a1-8178-820c-e6eeb11b15c1 "## New Section\nContent here"
    notion-api add-todo 31e2be82-a3a1-8178-820c-e6eeb11b15c1 "Follow up with John"
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from typing import Any

API_KEY = os.environ.get("NOTION_API_KEY", "")
API_VERSION = "2022-06-28"
BASE_URL = "https://api.notion.com/v1"


def _request(method: str, path: str, body: dict | None = None) -> dict:
    """Make an authenticated Notion API request."""
    if not API_KEY:
        print("Error: NOTION_API_KEY not set in environment", file=sys.stderr)
        sys.exit(1)

    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {API_KEY}")
    req.add_header("Notion-Version", API_VERSION)
    if data:
        req.add_header("Content-Type", "application/json")

    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"Notion API error {e.code}: {err}", file=sys.stderr)
        sys.exit(1)


def _rich_text(text: str, bold: bool = False) -> list[dict]:
    """Create a rich_text array from plain text."""
    rt: dict[str, Any] = {"type": "text", "text": {"content": text}}
    if bold:
        rt["annotations"] = {"bold": True}
    return [rt]


def _md_to_blocks(markdown: str) -> list[dict]:
    """Convert simple markdown to Notion blocks.

    Supports: ## headings, - bullets, [ ] todos, [x] todos, plain paragraphs.
    """
    blocks: list[dict] = []
    for line in markdown.strip().split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("### "):
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": _rich_text(stripped[4:])}
            })
        elif stripped.startswith("## "):
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": _rich_text(stripped[3:])}
            })
        elif stripped.startswith("# "):
            blocks.append({
                "object": "block",
                "type": "heading_1",
                "heading_1": {"rich_text": _rich_text(stripped[2:])}
            })
        elif stripped.startswith("- [x] ") or stripped.startswith("- [X] "):
            blocks.append({
                "object": "block",
                "type": "to_do",
                "to_do": {"rich_text": _rich_text(stripped[6:]), "checked": True}
            })
        elif stripped.startswith("- [ ] "):
            blocks.append({
                "object": "block",
                "type": "to_do",
                "to_do": {"rich_text": _rich_text(stripped[6:]), "checked": False}
            })
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _rich_text(stripped[2:])}
            })
        elif stripped.startswith("---"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        else:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": _rich_text(stripped)}
            })

    return blocks


def cmd_read(page_id: str) -> None:
    """Read a page's properties and content."""
    page = _request("GET", f"/pages/{page_id}")
    title = ""
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
            break

    print(f"Page: {title}")
    print(f"URL: {page.get('url', '')}")
    print(f"Last edited: {page.get('last_edited_time', '')}")
    print()

    # Get blocks
    blocks = _request("GET", f"/blocks/{page_id}/children?page_size=100")
    for block in blocks.get("results", []):
        btype = block["type"]
        if btype == "divider":
            print("---")
        elif btype == "child_database":
            print(f"[Database: {block['child_database'].get('title', '')}]")
        else:
            content = block.get(btype, {})
            rt = content.get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in rt)
            prefix = ""
            if btype == "heading_1":
                prefix = "# "
            elif btype == "heading_2":
                prefix = "## "
            elif btype == "heading_3":
                prefix = "### "
            elif btype == "bulleted_list_item":
                prefix = "- "
            elif btype == "numbered_list_item":
                prefix = "1. "
            elif btype == "to_do":
                checked = content.get("checked", False)
                prefix = "[x] " if checked else "[ ] "
            elif btype == "callout":
                icon = content.get("icon", {}).get("emoji", "")
                prefix = f"{icon} " if icon else "> "
            print(f"{prefix}{text}")


def cmd_list_blocks(page_id: str) -> None:
    """List all block IDs and types on a page."""
    blocks = _request("GET", f"/blocks/{page_id}/children?page_size=100")
    for block in blocks.get("results", []):
        btype = block["type"]
        rt = block.get(btype, {}).get("rich_text", [])
        preview = "".join(t.get("plain_text", "") for t in rt)[:60]
        print(f"{block['id']}  {btype:20s}  {preview}")


def cmd_append(page_id: str, markdown: str) -> None:
    """Append markdown content as new blocks to a page."""
    blocks = _md_to_blocks(markdown)
    if not blocks:
        print("No content to append.")
        return

    result = _request("PATCH", f"/blocks/{page_id}/children", {"children": blocks})
    count = len(result.get("results", []))
    print(f"Appended {count} blocks to page.")


def cmd_replace(page_id: str, markdown: str) -> None:
    """Replace all content on a page with new markdown."""
    # Delete existing blocks
    existing = _request("GET", f"/blocks/{page_id}/children?page_size=100")
    for block in existing.get("results", []):
        if block["type"] != "child_database":  # Don't delete databases
            try:
                _request("DELETE", f"/blocks/{block['id']}")
            except SystemExit:
                pass  # Skip blocks that can't be deleted

    # Append new content
    cmd_append(page_id, markdown)


def cmd_add_todo(page_id: str, text: str, checked: bool = False) -> None:
    """Add a single todo item to a page."""
    blocks = [{
        "object": "block",
        "type": "to_do",
        "to_do": {"rich_text": _rich_text(text), "checked": checked}
    }]
    result = _request("PATCH", f"/blocks/{page_id}/children", {"children": blocks})
    status = "checked" if checked else "unchecked"
    print(f"Added todo ({status}): {text}")


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    page_id = sys.argv[2]

    if cmd == "read":
        cmd_read(page_id)
    elif cmd == "list-blocks":
        cmd_list_blocks(page_id)
    elif cmd == "append":
        if len(sys.argv) < 4:
            # Read from stdin
            text = sys.stdin.read()
        else:
            text = sys.argv[3]
        cmd_append(page_id, text)
    elif cmd == "replace":
        if len(sys.argv) < 4:
            text = sys.stdin.read()
        else:
            text = sys.argv[3]
        cmd_replace(page_id, text)
    elif cmd == "add-todo":
        if len(sys.argv) < 4:
            print("Usage: notion-api add-todo <page-id> <text> [--checked]")
            sys.exit(1)
        text = sys.argv[3]
        checked = "--checked" in sys.argv
        cmd_add_todo(page_id, text, checked)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
