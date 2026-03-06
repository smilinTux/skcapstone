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


def _validate_timestamp(value: str, field_name: str, result: dict) -> None:
    """Check that a string is a valid ISO 8601 timestamp.

    Args:
        value: The timestamp string to validate.
        field_name: Dotted field path for error messages.
        result: The result dict to append errors/warnings to.
    """
    from datetime import datetime

    if not isinstance(value, str) or not value.strip():
        result["warnings"].append(f"{field_name} is empty or not a string")
        return
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        result["errors"].append(
            f"{field_name} is not a valid ISO 8601 timestamp: {value!r}"
        )
        result["valid"] = False


def _validate_tags(tags, field_name: str, result: dict) -> None:
    """Validate that tags is a list of non-empty strings.

    Args:
        tags: The value to validate.
        field_name: Dotted field path for error messages.
        result: The result dict to append errors/warnings to.
    """
    if not isinstance(tags, list):
        result["errors"].append(f"{field_name} must be a list, got {type(tags).__name__}")
        result["valid"] = False
        return
    for i, tag in enumerate(tags):
        if not isinstance(tag, str):
            result["errors"].append(
                f"{field_name}[{i}] must be a string, got {type(tag).__name__}"
            )
            result["valid"] = False
        elif not tag.strip():
            result["warnings"].append(f"{field_name}[{i}] is an empty string")


def _validate_emotional_signature(emo: dict, prefix: str, result: dict) -> None:
    """Validate an emotional_signature / emotional_snapshot block.

    Args:
        emo: The emotional data dict.
        prefix: Dotted field prefix for error messages.
        result: The result dict to append errors/warnings to.
    """
    if not isinstance(emo, dict):
        result["warnings"].append(f"{prefix} should be a dict")
        return

    intensity = emo.get("intensity")
    if intensity is not None:
        if not isinstance(intensity, (int, float)):
            result["errors"].append(f"{prefix}.intensity must be a number")
            result["valid"] = False
        elif not (0.0 <= float(intensity) <= 10.0):
            result["warnings"].append(
                f"{prefix}.intensity={intensity} is outside the expected 0-10 range"
            )

    valence = emo.get("valence")
    if valence is not None:
        if not isinstance(valence, (int, float)):
            result["errors"].append(f"{prefix}.valence must be a number")
            result["valid"] = False
        elif not (-1.0 <= float(valence) <= 1.0):
            result["warnings"].append(
                f"{prefix}.valence={valence} is outside the expected -1 to 1 range"
            )

    labels = emo.get("labels", emo.get("emotions"))
    if labels is not None:
        _validate_tags(labels, f"{prefix}.labels", result)


def validate_seed_data(data: dict) -> dict:
    """Validate parsed seed data (dict) against the SKSeed schema.

    This is the core validation logic, usable without a file path.
    Both the CLI ``skseed validate`` command and the memory-store
    import pipeline call this function.

    Args:
        data: Parsed JSON seed data.

    Returns:
        Dict with valid (bool), errors (list), warnings (list),
        and fields (list) keys.
    """
    result = {"valid": True, "errors": [], "warnings": [], "fields": []}

    if not isinstance(data, dict):
        result["valid"] = False
        result["errors"].append("Top-level value must be a JSON object")
        return result

    # ---------------------------------------------------------------
    # Detect format: Cloud9 (seed_metadata) vs standard
    # ---------------------------------------------------------------
    is_cloud9 = "seed_metadata" in data

    if is_cloud9:
        result["fields"].append("seed_metadata (Cloud9 format)")
        meta = data.get("seed_metadata", {})

        # Required: seed_id (inside seed_metadata or top-level)
        seed_id = meta.get("seed_id") or data.get("seed_id")
        if seed_id:
            result["fields"].append("seed_id")
        else:
            result["valid"] = False
            result["errors"].append(
                "Missing required field: seed_id "
                "(checked seed_metadata.seed_id and top-level)"
            )

        # Required: version
        version = meta.get("version") or data.get("version")
        if version:
            result["fields"].append("version")
        else:
            result["valid"] = False
            result["errors"].append("Missing required field: version")

        # Timestamps
        if "created_at" in meta:
            _validate_timestamp(meta["created_at"], "seed_metadata.created_at", result)

        identity = data.get("identity", {})
        if isinstance(identity, dict) and "timestamp" in identity:
            _validate_timestamp(identity["timestamp"], "identity.timestamp", result)

        # Experience summary (Cloud9 keeps it in experience_summary.narrative)
        exp = data.get("experience_summary", {})
        if isinstance(exp, dict):
            result["fields"].append("experience_summary")
            narrative = exp.get("narrative", "")
            if not narrative or not str(narrative).strip():
                result["warnings"].append("experience_summary.narrative is empty")
            emo = exp.get("emotional_snapshot") or exp.get("emotional_signature")
            if emo:
                _validate_emotional_signature(
                    emo, "experience_summary.emotional_snapshot", result,
                )
        else:
            result["warnings"].append(
                "Missing recommended field: experience_summary"
            )

        # Germination prompt
        gp = data.get("germination_prompt")
        if gp:
            result["fields"].append("germination_prompt")
            if isinstance(gp, str) and not gp.strip():
                result["warnings"].append("germination_prompt is empty")
        else:
            result["warnings"].append("Missing recommended field: germination_prompt")

    else:
        # ---- Standard format ----
        # Required fields
        for field in ("seed_id", "version"):
            if field in data:
                result["fields"].append(field)
                if isinstance(data[field], str) and not data[field].strip():
                    result["errors"].append(f"Required field {field} is empty")
                    result["valid"] = False
            else:
                result["valid"] = False
                result["errors"].append(f"Missing required field: {field}")

        # Recommended fields
        for field in ("creator", "experience", "germination"):
            if field in data:
                result["fields"].append(field)
            else:
                result["warnings"].append(f"Missing recommended field: {field}")

        # Creator structure
        creator = data.get("creator")
        if isinstance(creator, dict):
            if not creator.get("model") and not creator.get("instance"):
                result["warnings"].append(
                    "creator should have at least 'model' or 'instance'"
                )

        # Experience validation
        exp = data.get("experience", {})
        if isinstance(exp, dict):
            summary = exp.get("summary", "")
            if not summary or not str(summary).strip():
                result["errors"].append("experience.summary is empty or missing")
                result["valid"] = False

            emo = exp.get("emotional_signature")
            if emo:
                _validate_emotional_signature(
                    emo, "experience.emotional_signature", result,
                )

            key_claims = exp.get("key_claims")
            if key_claims is not None:
                _validate_tags(key_claims, "experience.key_claims", result)

        # Germination
        germ = data.get("germination", {})
        if isinstance(germ, dict):
            prompt = germ.get("prompt", "")
            if not prompt or not str(prompt).strip():
                result["warnings"].append("germination.prompt is empty")

    # ---------------------------------------------------------------
    # Common validations (both formats)
    # ---------------------------------------------------------------

    # metadata.tags
    metadata = data.get("metadata", {})
    if isinstance(metadata, dict):
        tags = metadata.get("tags")
        if tags is not None:
            _validate_tags(tags, "metadata.tags", result)
        ingested_at = metadata.get("ingested_at")
        if ingested_at:
            _validate_timestamp(ingested_at, "metadata.ingested_at", result)

    # lineage
    lineage = data.get("lineage")
    if lineage is not None:
        if not isinstance(lineage, list):
            result["errors"].append("lineage must be a list")
            result["valid"] = False
        else:
            for i, entry in enumerate(lineage):
                if not isinstance(entry, (str, dict)):
                    result["errors"].append(
                        f"lineage[{i}] must be a string or object, "
                        f"got {type(entry).__name__}"
                    )
                    result["valid"] = False

    # integrity checksum format
    integrity = data.get("integrity", {})
    if isinstance(integrity, dict):
        checksum = integrity.get("checksum", "")
        if checksum and ":" not in checksum:
            result["warnings"].append(
                "integrity.checksum should use 'algorithm:hex' format "
                f"(e.g. sha256:abc...), got: {checksum!r}"
            )

    return result


def validate_seed_file(path: str) -> dict:
    """Validate a seed.json file.

    Reads the file, parses JSON, and delegates to ``validate_seed_data``
    for schema-level checks.

    Args:
        path: Path to the seed.json file.

    Returns:
        Dict with valid (bool), errors (list), warnings (list),
        and fields (list) keys.
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

    return validate_seed_data(data)


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
