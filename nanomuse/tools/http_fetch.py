"""A plain HTTP client for the responses Playwright's request context cannot read.

Google answers a browser-looking request with a ~6 KB ``connection-allowlist-report-only``
header (YouTube and YouTube Studio both do). Playwright's request context is Node-based
and its parser gives up long before that — "Header overflow" — while Python's
``http.client`` reads a header line up to 64 KB without complaint. This client speaks
HTTP/1.1 over plain sockets through the split proxy, carrying the browser's cookies so
the request stays signed in.

It is a fallback: the normal path is still Playwright's request context.
"""

from __future__ import annotations

import asyncio
import http.client
import socket
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from nanomuse.logger import logger
from nanomuse.tools.net_proxy import load_config, parse_upstream, route_for, socks5_connect

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "cookie",
    "content-length",
}


def _open_socket(host: str, port: int, workspace: Path | None, timeout: float) -> socket.socket:
    """Reach the origin, through the upstream proxy when the host is on the proxy list."""
    cfg = load_config(workspace)
    upstream = parse_upstream(str(cfg.get("upstream") or ""))
    if route_for(host, cfg) == "proxy" and upstream[1]:
        kind, up_host, up_port, _user, _password = upstream
        if kind == "socks5":
            return socks5_connect(upstream, host, port)
        sock = socket.create_connection((up_host, up_port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
        status = head.split(b"\r\n")[0] if head else b""
        if b" 200" not in status:
            sock.close()
            raise OSError(
                f"upstream refused {host}:{port}: {status.decode('latin1', 'replace')[:80]}"
            )
        return sock
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    return sock


def _fetch_sync(
    url: str,
    method: str,
    headers: dict[str, str] | None,
    body: str | None,
    cookies: list[dict[str, Any]] | None,
    user_agent: str | None,
    workspace: Path | None,
    timeout: float,
    verify_tls: bool = True,
) -> dict[str, Any]:
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {scheme}")
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    target = urlunsplit(("", "", parts.path or "/", parts.query, ""))

    body_bytes = (body or "").encode("utf-8")
    lines = [f"{method.upper()} {target} HTTP/1.1", f"Host: {parts.netloc}"]
    if user_agent:
        lines.append(f"User-Agent: {user_agent}")
    if cookies:
        jar = "; ".join(f"{c.get('name')}={c.get('value')}" for c in cookies if c.get("name"))
        if jar:
            lines.append(f"Cookie: {jar}")
    for key, value in (headers or {}).items():
        if key.lower() in HOP_BY_HOP:
            continue
        lines.append(f"{key}: {value}")
    if body:
        lines.append(f"Content-Length: {len(body_bytes)}")
    lines.append("Connection: close")

    sock = _open_socket(host, port, workspace, timeout)
    try:
        if scheme == "https":
            ctx = ssl.create_default_context()
            if not verify_tls:
                # Only for a host whose certificate the machine cannot check; the
                # cookies travel over this connection, so the default is to verify.
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.sendall("\r\n".join(lines).encode("latin1", "replace") + b"\r\n\r\n")
        if body_bytes:
            sock.sendall(body_bytes)
        response = http.client.HTTPResponse(sock)
        response.begin()
        payload = response.read()
        charset = response.charset or "utf-8"
        return {
            "status": response.status,
            "headers": dict(response.getheaders()),
            "body": payload.decode(charset, "replace"),
            "url": url,
        }
    finally:
        try:
            sock.close()
        except OSError:
            pass


async def fetch(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    cookies: list[dict[str, Any]] | None = None,
    user_agent: str | None = None,
    workspace: Path | None = None,
    timeout: float = 30.0,
    verify_tls: bool = True,
) -> dict[str, Any]:
    """One request, read with a parser that tolerates large header blocks."""
    logger.info("http_fetch: plain client for {} {}", method.upper(), url)
    return await asyncio.to_thread(
        _fetch_sync,
        url,
        method,
        headers,
        body,
        cookies,
        user_agent,
        workspace,
        timeout,
        verify_tls,
    )
