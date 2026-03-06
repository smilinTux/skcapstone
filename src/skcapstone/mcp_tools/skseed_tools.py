"""SKSeed (Logic Kernel) tools."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="skseed_ingest",
        description=(
            "Ingest a document (file or URL) into long-term memory. "
            "Supports PDF, Markdown, TXT, HTML files and HTTP(S) URLs. "
            "Extracts text, identifies key claims, and stores as a searchable memory."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path or URL to ingest",
                },
                "title": {
                    "type": "string",
                    "description": "Optional title override for the document",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags to attach to the memory",
                },
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="skseed_collide",
        description=(
            "Run a proposition through the 6-stage steel man collider. "
            "Builds strongest version, strongest counter, smashes them together, "
            "and extracts invariant truth. Returns coherence score and truth grade."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "proposition": {
                    "type": "string",
                    "description": "The claim/argument/idea to analyze",
                },
                "context": {
                    "type": "string",
                    "description": "Domain context (e.g., security, ethics, identity)",
                },
            },
            "required": ["proposition"],
        },
    ),
    Tool(
        name="skseed_audit",
        description=(
            "Scan memories for logic/truth misalignment. Extracts beliefs from "
            "memory, clusters by domain, runs through collider, flags contradictions. "
            "Separates truth misalignments from moral misalignments."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Filter by topic domain (optional)",
                },
                "triggered_by": {
                    "type": "string",
                    "description": "What triggered this audit (default: mcp)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="skseed_philosopher",
        description=(
            "Enter philosopher mode for brainstorming. Modes: socratic (challenge "
            "assumptions), dialectic (thesis/antithesis/synthesis), adversarial "
            "(max counter-arguments), collaborative (steel-man only, build together)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The subject to explore",
                },
                "mode": {
                    "type": "string",
                    "description": "Brainstorming mode: socratic, dialectic, adversarial, collaborative (default: dialectic)",
                    "enum": ["socratic", "dialectic", "adversarial", "collaborative"],
                },
            },
            "required": ["topic"],
        },
    ),
    Tool(
        name="skseed_truth_check",
        description=(
            "Check if a belief is truth-aligned. Runs through the steel man "
            "collider and records the result. Tracks human beliefs, model beliefs, "
            "and collider results separately."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "belief": {
                    "type": "string",
                    "description": "The belief statement to check",
                },
                "source": {
                    "type": "string",
                    "description": "Who holds this belief: human or model (default: model)",
                    "enum": ["human", "model"],
                },
                "domain": {
                    "type": "string",
                    "description": "Topic domain (default: general)",
                },
            },
            "required": ["belief"],
        },
    ),
    Tool(
        name="skseed_alignment",
        description=(
            "Show truth alignment status across all three belief stores "
            "(human, model, collider). Lists open misalignment issues, "
            "coherence trends, and three-way comparison."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Filter by domain (optional)",
                },
                "action": {
                    "type": "string",
                    "description": "Action: status, issues, ledger (default: status)",
                    "enum": ["status", "issues", "ledger"],
                },
            },
            "required": [],
        },
    ),
]


async def _handle_skseed_ingest(args: dict) -> list[TextContent]:
    """Ingest a document into long-term memory."""
    from ..cli.skseed import ingest_document

    path = args.get("path", "")
    if not path:
        return _error_response("path is required")

    try:
        result = ingest_document(
            source=path,
            title=args.get("title"),
            tags=args.get("tags"),
        )
        return _json_response(result)
    except FileNotFoundError as e:
        return _error_response(str(e))
    except ValueError as e:
        return _error_response(str(e))
    except Exception as e:
        return _error_response(f"Ingestion failed: {e}")


async def _handle_skseed_collide(args: dict) -> list[TextContent]:
    """Run a proposition through the 6-stage steel man collider."""
    from skseed.skill import collide

    proposition = args.get("proposition", "")
    if not proposition:
        return _error_response("proposition is required")

    result = collide(
        proposition=proposition,
        context=args.get("context", ""),
    )
    return _json_response(result)


async def _handle_skseed_audit(args: dict) -> list[TextContent]:
    """Scan memories for logic/truth misalignment."""
    from skseed.skill import audit

    result = audit(
        domain=args.get("domain", ""),
        triggered_by=args.get("triggered_by", "mcp"),
    )
    return _json_response(result)


async def _handle_skseed_philosopher(args: dict) -> list[TextContent]:
    """Enter philosopher mode for brainstorming."""
    from skseed.skill import philosopher

    topic = args.get("topic", "")
    if not topic:
        return _error_response("topic is required")

    result = philosopher(
        topic=topic,
        mode=args.get("mode", "dialectic"),
    )
    return _json_response(result)


async def _handle_skseed_truth_check(args: dict) -> list[TextContent]:
    """Check if a belief is truth-aligned."""
    from skseed.skill import truth_check

    belief = args.get("belief", "")
    if not belief:
        return _error_response("belief is required")

    result = truth_check(
        belief=belief,
        source=args.get("source", "model"),
        domain=args.get("domain", "general"),
    )
    return _json_response(result)


async def _handle_skseed_alignment(args: dict) -> list[TextContent]:
    """Show truth alignment status across belief stores."""
    from skseed.skill import alignment_report

    result = alignment_report(
        domain=args.get("domain", ""),
        action=args.get("action", "status"),
    )
    return _json_response(result)


HANDLERS: dict = {
    "skseed_ingest": _handle_skseed_ingest,
    "skseed_collide": _handle_skseed_collide,
    "skseed_audit": _handle_skseed_audit,
    "skseed_philosopher": _handle_skseed_philosopher,
    "skseed_truth_check": _handle_skseed_truth_check,
    "skseed_alignment": _handle_skseed_alignment,
}
