"""SKSeed document ingestion CLI commands.

Turns documents (PDF, Markdown, TXT, HTML, URLs) into long-term memories
via the SKMemory store. Also validates seed.json files.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import click

from ._common import AGENT_HOME, console


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    """Minimal HTML tag stripper using stdlib html.parser."""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def _strip_html(html: str) -> str:
    """Remove HTML tags and return plain text."""
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()


def _extract_title_from_content(content: str, filename: str) -> str:
    """Extract a title from content (first heading) or fall back to filename."""
    # Try markdown heading
    for line in content.splitlines()[:20]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    # Fall back to filename without extension
    return Path(filename).stem


def _extract_key_claims(content: str, max_claims: int = 5) -> list[str]:
    """Extract key claims / sentences from content.

    Simple heuristic: pick the first N non-trivial sentences.
    """
    sentences = re.split(r"(?<=[.!?])\s+", content.strip())
    claims: list[str] = []
    for s in sentences:
        s = s.strip()
        if len(s) > 30 and not s.startswith("#"):
            claims.append(s)
            if len(claims) >= max_claims:
                break
    return claims


def _read_pdf(path: Path) -> str:
    """Read text from a PDF file. Uses PyMuPDF if available, else raw read."""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(path))
        pages = []
        for page in doc:
            pages.append(page.get_text())
        doc.close()
        return "\n".join(pages)
    except ImportError:
        # Fallback: read raw bytes and try to extract printable text
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="ignore")
        # Strip non-printable noise
        return "".join(ch for ch in text if ch.isprintable() or ch in "\n\r\t")


def _read_file(path: Path) -> tuple[str, str]:
    """Read a local file and return (content, content_type).

    Returns:
        Tuple of (extracted text, detected type string).
    """
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return _read_pdf(path), "pdf"
    elif suffix == ".html" or suffix == ".htm":
        raw = path.read_text(encoding="utf-8", errors="replace")
        return _strip_html(raw), "html"
    elif suffix in (".md", ".markdown"):
        return path.read_text(encoding="utf-8", errors="replace"), "markdown"
    elif suffix == ".txt":
        return path.read_text(encoding="utf-8", errors="replace"), "text"
    elif suffix == ".json":
        return path.read_text(encoding="utf-8", errors="replace"), "json"
    else:
        # Best-effort: read as text
        return path.read_text(encoding="utf-8", errors="replace"), "text"


def _fetch_url(url: str) -> tuple[str, str]:
    """Fetch a URL and return (content, content_type)."""
    from urllib.request import urlopen, Request

    req = Request(url, headers={"User-Agent": "skcapstone-skseed/1.0"})
    with urlopen(req, timeout=30) as resp:
        ctype = resp.headers.get("Content-Type", "text/plain")
        raw = resp.read().decode("utf-8", errors="replace")

    if "html" in ctype:
        return _strip_html(raw), "html"
    return raw, "text"


def _get_memory_store():
    """Get a MemoryStore instance for the current agent."""
    from skmemory.store import MemoryStore

    return MemoryStore()


def _generate_seed_json(
    title: str,
    content: str,
    claims: list[str],
    source_path: str,
    content_type: str,
    tags: Optional[list[str]] = None,
) -> dict:
    """Build a seed.json dict from extracted document content."""
    from datetime import datetime, timezone

    return {
        "seed_id": f"doc-{Path(source_path).stem}",
        "version": "1.0",
        "creator": {"model": "skseed-ingest", "instance": "cli"},
        "experience": {
            "summary": content[:2000],
            "key_claims": claims,
        },
        "germination": {
            "prompt": f"Document ingested from {source_path}: {title}",
        },
        "metadata": {
            "source_path": source_path,
            "content_type": content_type,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "tags": tags or [],
        },
    }


def ingest_document(
    source: str,
    title: Optional[str] = None,
    tags: Optional[list[str]] = None,
    output_seed: Optional[str] = None,
) -> dict:
    """Core ingestion logic shared by CLI and MCP.

    Args:
        source: File path or URL to ingest.
        title: Optional title override.
        tags: Optional tags for the memory.
        output_seed: Optional path to write seed.json to.

    Returns:
        Dict with memory_id, title, summary, and seed_path keys.
    """
    # Determine if source is URL or file
    parsed = urlparse(source)
    is_url = parsed.scheme in ("http", "https")

    if is_url:
        content, content_type = _fetch_url(source)
        filename = parsed.path.split("/")[-1] or "web-document"
    else:
        path = Path(source).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        content, content_type = _read_file(path)
        filename = path.name

    if not content.strip():
        raise ValueError("No content could be extracted from the source")

    # Determine title
    doc_title = title or _extract_title_from_content(content, filename)

    # Extract key claims
    claims = _extract_key_claims(content)

    # Build seed JSON
    seed_data = _generate_seed_json(
        title=doc_title,
        content=content,
        claims=claims,
        source_path=source,
        content_type=content_type,
        tags=tags,
    )

    # Write seed.json if requested
    seed_path = output_seed
    if seed_path:
        out = Path(seed_path)
        out.write_text(json.dumps(seed_data, indent=2), encoding="utf-8")

    # Store as long-term memory
    all_tags = ["seed", "document", f"type:{content_type}"]
    if tags:
        all_tags.extend(tags)

    store = _get_memory_store()
    memory = store.snapshot(
        title=f"Document: {doc_title}",
        content=content[:10000],  # Cap content for storage
        layer="long-term",
        source="skseed-ingest",
        source_ref=source,
        tags=all_tags,
        metadata={
            "key_claims": claims,
            "content_type": content_type,
            "original_length": len(content),
        },
    )

    return {
        "memory_id": memory.id,
        "title": doc_title,
        "summary": content[:200].strip(),
        "seed_path": seed_path,
        "claims_count": len(claims),
    }


def validate_seed_file(path: str) -> dict:
    """Validate a seed.json file.

    Args:
        path: Path to the seed.json file.

    Returns:
        Dict with valid (bool), errors (list), and warnings (list).
    """
    result = {"valid": True, "errors": [], "warnings": [], "fields": []}
    file_path = Path(path).expanduser().resolve()

    if not file_path.exists():
        result["valid"] = False
        result["errors"].append(f"File not found: {file_path}")
        return result

    # Check JSON validity
    try:
        raw = file_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        result["valid"] = False
        result["errors"].append(f"Invalid JSON: {e}")
        return result

    if not isinstance(data, dict):
        result["valid"] = False
        result["errors"].append("Top-level value must be a JSON object")
        return result

    # Check required fields
    required = ["seed_id", "version"]
    for field in required:
        if field in data:
            result["fields"].append(field)
        else:
            result["valid"] = False
            result["errors"].append(f"Missing required field: {field}")

    # Check recommended fields
    recommended = ["creator", "experience", "germination"]
    for field in recommended:
        if field in data:
            result["fields"].append(field)
        else:
            result["warnings"].append(f"Missing recommended field: {field}")

    # Check alternative Cloud9 format
    if "seed_metadata" in data:
        result["fields"].append("seed_metadata (Cloud9 format)")
        if "seed_id" not in result["errors"]:
            # Cloud9 format has seed_id inside seed_metadata
            meta = data.get("seed_metadata", {})
            if "seed_id" not in meta and "seed_id" not in data:
                result["warnings"].append(
                    "Cloud9 format detected but seed_metadata.seed_id missing"
                )

    # Check experience has summary
    exp = data.get("experience", {})
    if isinstance(exp, dict) and not exp.get("summary"):
        result["warnings"].append("experience.summary is empty")

    return result


# ---------------------------------------------------------------------------
# CLI command group
# ---------------------------------------------------------------------------

def register_skseed_commands(main: click.Group) -> None:
    """Register the skseed command group."""

    @main.group()
    def skseed():
        """SKSeed — document ingestion and seed management.

        Turn documents into memories. Validate seed files.
        """

    @skseed.command("ingest")
    @click.argument("source")
    @click.option("--title", "-t", default=None, help="Override document title.")
    @click.option(
        "--tag", "-g", multiple=True, help="Tags to attach to the memory."
    )
    @click.option(
        "--output-seed", "-o", default=None,
        help="Path to write seed.json output.",
    )
    def ingest_cmd(source, title, tag, output_seed):
        """Ingest a document (file or URL) into memory.

        Supports: .pdf, .md, .txt, .html files and http(s) URLs.
        Extracts text, identifies key claims, stores as long-term memory.
        """
        try:
            result = ingest_document(
                source=source,
                title=title,
                tags=list(tag) if tag else None,
                output_seed=output_seed,
            )
            console.print(f"Ingested: {result['title']} -> {result['memory_id']}")
            if result.get("seed_path"):
                console.print(f"  Seed written to: {result['seed_path']}")
            console.print(f"  Claims extracted: {result['claims_count']}")
        except FileNotFoundError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise SystemExit(1)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise SystemExit(1)
        except Exception as e:
            console.print(f"[red]Ingestion failed:[/red] {e}")
            raise SystemExit(1)

    @skseed.command("validate")
    @click.argument("file")
    def validate_cmd(file):
        """Validate a seed.json file.

        Checks JSON validity, required fields, and recommended structure.
        """
        result = validate_seed_file(file)

        if result["valid"]:
            console.print("[green]VALID[/green] seed file")
        else:
            console.print("[red]INVALID[/red] seed file")

        if result["fields"]:
            console.print(f"  Fields found: {', '.join(result['fields'])}")

        for err in result["errors"]:
            console.print(f"  [red]ERROR:[/red] {err}")

        for warn in result["warnings"]:
            console.print(f"  [yellow]WARNING:[/yellow] {warn}")

        if not result["valid"]:
            raise SystemExit(1)
