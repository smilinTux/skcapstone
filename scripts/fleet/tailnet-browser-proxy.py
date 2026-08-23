#!/usr/bin/env python3
"""Loopback-only HTTP proxy for browser access to Tailscale peers."""

from __future__ import annotations

import argparse
import ipaddress
import json
import selectors
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

TAILNET_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

PAC_TEMPLATE = """function FindProxyForURL(url, host) {{
  var proxy = "PROXY 127.0.0.1:{proxy_port}";
  if (isPlainHostName(host) && host != "localhost") return proxy;
  if (dnsDomainIs(host, ".ts.net")) return proxy;
  if (isInNet(host, "100.64.0.0", "255.192.0.0")) return proxy;
  return "DIRECT";
}}
"""

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def is_tailnet_address(value: str) -> bool:
    """Return whether an IP belongs to the Tailscale overlay ranges."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in TAILNET_NETWORKS)


def resolve_tailnet(host: str, port: int) -> list[tuple[int, tuple[object, ...]]]:
    """Resolve a host and return only exact Tailscale overlay destinations."""
    results: list[tuple[int, tuple[object, ...]]] = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        if is_tailnet_address(str(sockaddr[0])):
            results.append((family, sockaddr))
    return results


def open_tailnet_connection(host: str, port: int, timeout: float = 10.0) -> socket.socket:
    """Connect to a resolved tailnet address without a second DNS lookup."""
    errors: list[str] = []
    for family, sockaddr in resolve_tailnet(host, port):
        upstream = socket.socket(family, socket.SOCK_STREAM)
        upstream.settimeout(timeout)
        try:
            upstream.connect(sockaddr)
            return upstream
        except OSError as exc:
            errors.append(str(exc))
            upstream.close()
    detail = "; ".join(errors) if errors else "destination is outside tailnet ranges"
    raise OSError(detail)


def parse_authority(authority: str, default_port: int) -> tuple[str, int]:
    """Parse a CONNECT or Host authority safely, including IPv6 literals."""
    parsed = urlsplit(f"//{authority}")
    if not parsed.hostname:
        raise ValueError("missing destination host")
    port = parsed.port or default_port
    if port < 1 or port > 65535:
        raise ValueError("invalid destination port")
    return parsed.hostname, port


class TailnetProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SKCapstoneTailnetProxy/1"

    def do_CONNECT(self) -> None:
        try:
            host, port = parse_authority(self.path, 443)
            upstream = open_tailnet_connection(host, port)
        except (OSError, ValueError) as exc:
            self.send_error(403, f"Tailnet destination denied: {exc}")
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        self._tunnel(upstream)

    def do_GET(self) -> None:
        self._forward_http()

    def do_HEAD(self) -> None:
        self._forward_http()

    def do_POST(self) -> None:
        self._forward_http()

    def do_PUT(self) -> None:
        self._forward_http()

    def do_DELETE(self) -> None:
        self._forward_http()

    def do_OPTIONS(self) -> None:
        self._forward_http()

    def _forward_http(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.scheme and parsed.scheme.lower() != "http":
            self.send_error(400, "Only HTTP requests and HTTPS CONNECT are supported")
            return
        authority = parsed.netloc or self.headers.get("Host", "")
        try:
            host, port = parse_authority(authority, 80)
            upstream = open_tailnet_connection(host, port)
        except (OSError, ValueError) as exc:
            self.send_error(403, f"Tailnet destination denied: {exc}")
            return

        path = self.path
        if parsed.scheme:
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
        headers = [f"{self.command} {path} HTTP/1.1\r\n"]
        for name, value in self.headers.items():
            if name.lower() not in HOP_BY_HOP:
                headers.append(f"{name}: {value}\r\n")
        headers.append("Connection: close\r\n\r\n")
        upstream.sendall("".join(headers).encode("iso-8859-1"))

        length = int(self.headers.get("Content-Length", "0"))
        if length:
            upstream.sendall(self.rfile.read(length))
        try:
            while data := upstream.recv(65536):
                self.connection.sendall(data)
        finally:
            upstream.close()
            self.close_connection = True

    def _tunnel(self, upstream: socket.socket) -> None:
        selector = selectors.DefaultSelector()
        selector.register(self.connection, selectors.EVENT_READ, upstream)
        selector.register(upstream, selectors.EVENT_READ, self.connection)
        try:
            while True:
                events = selector.select(timeout=60)
                if not events:
                    return
                for key, _ in events:
                    data = key.fileobj.recv(65536)
                    if not data:
                        return
                    key.data.sendall(data)
        finally:
            selector.close()
            upstream.close()

    def log_message(self, message: str, *args: object) -> None:
        print(f"proxy client={self.client_address[0]} {message % args}", flush=True)


class PacHandler(BaseHTTPRequestHandler):
    proxy_port = 1055

    def do_GET(self) -> None:
        if self.path == "/healthz":
            body = json.dumps({"ok": True, "proxy_port": self.proxy_port}).encode()
            content_type = "application/json"
        elif self.path == "/proxy.pac":
            body = PAC_TEMPLATE.format(proxy_port=self.proxy_port).encode()
            content_type = "application/x-ns-proxy-autoconfig"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message: str, *args: object) -> None:
        print(f"pac client={self.client_address[0]} {message % args}", flush=True)


class LoopbackServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy-port", type=int, default=1055)
    parser.add_argument("--pac-port", type=int, default=1056)
    args = parser.parse_args()
    PacHandler.proxy_port = args.proxy_port
    proxy = LoopbackServer(("127.0.0.1", args.proxy_port), TailnetProxyHandler)
    pac = ThreadingHTTPServer(("127.0.0.1", args.pac_port), PacHandler)
    threads = [
        threading.Thread(target=proxy.serve_forever, daemon=True),
        threading.Thread(target=pac.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    print(
        f"tailnet browser proxy listening on 127.0.0.1:{args.proxy_port}; "
        f"PAC on 127.0.0.1:{args.pac_port}",
        flush=True,
    )
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.shutdown()
        pac.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
