"""File transfer tools."""

from __future__ import annotations

from pathlib import Path

from mcp.types import TextContent, Tool

from ._helpers import _get_agent_name, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="file_send",
        description=(
            "Prepare a file for encrypted transfer to another agent. "
            "Splits into 256KB chunks, encrypts with KMS key, writes to outbox."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to send",
                },
                "recipient": {
                    "type": "string",
                    "description": "Recipient agent name",
                },
                "encrypt": {
                    "type": "boolean",
                    "description": "Whether to encrypt chunks (default: true)",
                },
            },
            "required": ["file_path", "recipient"],
        },
    ),
    Tool(
        name="file_receive",
        description=(
            "Receive and reassemble a file transfer. "
            "Decrypts chunks, verifies integrity (SHA-256), writes assembled file."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "transfer_id": {
                    "type": "string",
                    "description": "The transfer ID to receive",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Output directory (optional, defaults to inbox)",
                },
            },
            "required": ["transfer_id"],
        },
    ),
    Tool(
        name="file_list",
        description=(
            "List all file transfers with progress info. "
            "Shows filename, size, direction, progress for each transfer."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "description": "Filter: 'send' or 'receive' (omit for all)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="file_status",
        description=(
            "Get file transfer subsystem status: outbox/inbox/completed counts."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


async def _handle_file_send(args: dict) -> list[TextContent]:
    """Send a file to another agent."""
    from ..file_transfer import FileTransfer

    home = _home()
    agent_name = _get_agent_name(home)
    ft = FileTransfer(home, agent_name=agent_name)
    ft.initialize()

    file_path = Path(args["file_path"])
    manifest = ft.send(
        file_path,
        recipient=args["recipient"],
        encrypt=args.get("encrypt", True),
    )
    return _json_response({
        "transfer_id": manifest.transfer_id,
        "filename": manifest.filename,
        "file_size": manifest.file_size,
        "total_chunks": manifest.total_chunks,
        "sender": manifest.sender,
        "recipient": manifest.recipient,
        "file_sha256": manifest.file_sha256[:16] + "...",
    })


async def _handle_file_receive(args: dict) -> list[TextContent]:
    """Receive and reassemble a file transfer."""
    from ..file_transfer import FileTransfer

    home = _home()
    agent_name = _get_agent_name(home)
    ft = FileTransfer(home, agent_name=agent_name)
    ft.initialize()

    output_dir = Path(args["output_dir"]) if args.get("output_dir") else None
    output_path = ft.receive(args["transfer_id"], output_dir=output_dir)
    return _json_response({
        "transfer_id": args["transfer_id"],
        "output_path": str(output_path),
        "file_size": output_path.stat().st_size,
    })


async def _handle_file_list(args: dict) -> list[TextContent]:
    """List file transfers."""
    from ..file_transfer import FileTransfer

    home = _home()
    ft = FileTransfer(home, agent_name=_get_agent_name(home))
    ft.initialize()

    transfers = ft.list_transfers(direction=args.get("direction"))
    return _json_response([
        {
            "transfer_id": t.transfer_id,
            "filename": t.filename,
            "file_size": t.file_size,
            "direction": t.direction,
            "progress": round(t.progress, 2),
            "chunks_done": t.chunks_done,
            "total_chunks": t.total_chunks,
            "sender": t.sender,
            "recipient": t.recipient,
        }
        for t in transfers
    ])


async def _handle_file_status(_args: dict) -> list[TextContent]:
    """Get file transfer subsystem status."""
    from ..file_transfer import FileTransfer

    home = _home()
    ft = FileTransfer(home, agent_name=_get_agent_name(home))
    ft.initialize()
    return _json_response(ft.status())


HANDLERS: dict = {
    "file_send": _handle_file_send,
    "file_receive": _handle_file_receive,
    "file_list": _handle_file_list,
    "file_status": _handle_file_status,
}
