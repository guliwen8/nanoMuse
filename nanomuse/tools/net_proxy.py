"""A split proxy that sits in front of the browser: some domains leave through an upstream
proxy, everything else goes straight out.

Why it exists: the machine the agent runs on sits in China. The Chinese platforms
(Douyin, Xiaohongshu, Xianyu, Bilibili...) refuse a foreign exit IP and start throwing
"illegal access" at you, while YouTube is unreachable without one. A single global proxy
breaks the first group; a single direct connection breaks the second. So the browser — and
the request context that shares its proxy — points here, and this decides per host.

The upstream is an ordinary HTTP proxy (Clash's mixed port, for instance) that forwards
``CONNECT``. The configuration lives in the workspace as JSON and is re-read on every
connection, so it can be changed without restarting anything:

    <workspace>/proxy/split-proxy.json

    {
      "listen": "127.0.0.1:18080",
      "upstream": "192.168.10.7:7897",
      "proxy_domains": ["youtube.com", "googlevideo.com", "googleapis.com"],
      "direct_domains": [],
      "default_route": "direct"
    }

``proxy_domains`` entries match the domain itself and everything under it; ``*`` matches
everything. ``direct_domains`` wins over ``proxy_domains`` and the default route.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from nanomuse.logger import logger

CONFIG_RELATIVE = Path("proxy") / "split-proxy.json"

DEFAULT_LISTEN = "127.0.0.1:18080"
DEFAULT_UPSTREAM = ""
DEFAULT_PROXY_DOMAINS = [
    "youtube.com",
    "googlevideo.com",
    "youtubei.googleapis.com",
    "ytimg.com",
    "gstatic.com",
    "googleapis.com",
    "google.com",
    "ggpht.com",
    "googleusercontent.com",
    "googletagmanager.com",
]
DEFAULT_DIRECT_DOMAINS: list[str] = []

HOP_BY_HOP = {
    "proxy-connection",
    "proxy-authorization",
    "connection",
    "keep-alive",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

_state: dict[str, Any] = {"server": None, "url": "", "workspace": None}


# ---------------------------------------------------------------------- configuration
def config_path(workspace: Path | None) -> Path | None:
    if workspace is None:
        return None
    return Path(workspace) / CONFIG_RELATIVE


def load_config(workspace: Path | None = None) -> dict[str, Any]:
    """The split-proxy settings, falling back to the built-in defaults."""
    cfg: dict[str, Any] = {
        "listen": DEFAULT_LISTEN,
        "upstream": DEFAULT_UPSTREAM,
        "proxy_domains": list(DEFAULT_PROXY_DOMAINS),
        "direct_domains": list(DEFAULT_DIRECT_DOMAINS),
        "default_route": "direct",
    }
    path = config_path(workspace)
    if path is not None and path.exists():
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in cfg})
        except (OSError, ValueError) as exc:  # noqa: BLE001
            logger.warning("net_proxy: cannot read {}: {}", path, exc)
    return cfg


def match_domain(host: str, patterns: list[str]) -> bool:
    """True when ``host`` is one of ``patterns`` or sits under one of them."""
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    for pattern in patterns or []:
        pattern = str(pattern).strip().lower()
        if not pattern:
            continue
        if pattern == "*":
            return True
        pattern = pattern.removeprefix("*.").lstrip(".")
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def route_for(host: str, cfg: dict[str, Any]) -> str:
    """``"proxy"`` or ``"direct"`` for one host."""
    if match_domain(host, list(cfg.get("direct_domains") or [])):
        return "direct"
    if match_domain(host, list(cfg.get("proxy_domains") or [])):
        return "proxy"
    return "direct" if cfg.get("default_route", "direct") == "direct" else "proxy"



def _recv_exact(sock: socket.socket, count: int) -> bytes:
    """Read exactly ``count`` bytes — a SOCKS5 reply must not be read with ``recv(n)``."""
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise OSError("the peer closed the connection")
        buf += chunk
    return buf


def _address_type(host: str) -> int:
    """SOCKS5 address type: 1 for IPv4, 4 for IPv6, 3 for a name."""
    import ipaddress

    try:
        ipaddress.IPv4Address(host)
        return 1
    except ValueError:
        pass
    try:
        ipaddress.IPv6Address(host)
        return 4
    except ValueError:
        return 3


def parse_upstream(raw: str) -> tuple[str, str, int, str, str]:
    """``(kind, host, port, user, password)`` for an upstream proxy.

    ``socks5://user:pass@host:port`` → SOCKS5 (with authentication);
    ``host:port`` → an ordinary HTTP proxy that speaks ``CONNECT``.
    """
    raw = (raw or "").strip()
    if not raw:
        return ("", "", 0, "", "")
    kind = "http"
    rest = raw
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        kind = "socks5" if scheme.lower() in ("socks5", "socks5h") else "http"
    user = password = ""
    if "@" in rest:
        auth, rest = rest.rsplit("@", 1)
        user, _, password = auth.partition(":")
    host, _, port = rest.partition(":")
    try:
        port_num = int(port or (1080 if kind == "socks5" else 8080))
    except ValueError:
        port_num = 1080 if kind == "socks5" else 8080
    return (kind, host, port_num, user, password)


def socks5_connect(
    upstream: tuple[str, str, int, str, str], target_host: str, target_port: int
) -> socket.socket:
    """Open a SOCKS5 tunnel to the target through the upstream proxy."""
    _, host, port, user, password = upstream
    sock = socket.create_connection((host, port), timeout=20)
    sock.settimeout(30)
    if user:
        sock.sendall(b"\x05\x02\x00\x02")
        head = _recv_exact(sock, 2)
        if head[0] != 0x05 or _recv_exact(sock, 1)[0] != 0x02:
            sock.close()
            raise OSError("the SOCKS5 upstream refused the username/password method")
        user_raw = user.encode()
        pass_raw = password.encode()
        sock.sendall(bytes([1, len(user_raw)]) + user_raw + bytes([len(pass_raw)]) + pass_raw)
        reply = _recv_exact(sock, 2)
        if reply[1] != 0x00:
            sock.close()
            raise OSError("the SOCKS5 upstream rejected the credentials")
    else:
        sock.sendall(b"\x05\x01\x00")
        head = _recv_exact(sock, 2)
        if head[0] != 0x05:
            sock.close()
            raise OSError("the SOCKS5 upstream is not a SOCKS5 server")
        # mihomo answers with two bytes (05 00); the RFC sends a count first
        if head[1] == 0x00:
            method = 0x00
        else:
            methods = _recv_exact(sock, head[1])
            method = 0x00 if 0x00 in methods else (methods[0] if methods else 0xFF)
        if method != 0x00:
            sock.close()
            raise OSError("the SOCKS5 upstream did not accept the no-auth method")
    kind = _address_type(target_host)
    if kind == 1:
        payload = socket.inet_aton(target_host)
    elif kind == 4:
        payload = socket.inet_pton(socket.AF_INET6, target_host)
    else:
        raw = target_host.encode()
        payload = bytes([len(raw)]) + raw
    sock.sendall(
        b"\x05\x01\x00" + bytes([kind]) + payload + target_port.to_bytes(2, "big")
    )
    reply = _recv_exact(sock, 4)
    if reply[1] != 0x00:
        sock.close()
        raise OSError(f"the SOCKS5 upstream refused the target: {reply!r}")
    atyp = reply[3]
    if atyp == 1:
        _recv_exact(sock, 4)
    elif atyp == 3:
        _recv_exact(sock, _recv_exact(sock, 1)[0])
    elif atyp == 4:
        _recv_exact(sock, 16)
    _recv_exact(sock, 2)
    return sock

# ---------------------------------------------------------------------- the proxy itself
def _read_headers(sock: socket.socket) -> bytes:
    """Read until the end of the header block; return it whole."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            raise OSError("header block too large")
    return buf


def _read_exact(stream, count: int) -> bytes:
    """Read exactly ``count`` bytes; a raw socket stream can return short reads."""
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _tunnel(client: socket.socket, remote: socket.socket) -> None:
    """Shuttle bytes both ways until one side goes quiet."""
    worker = threading.Thread(target=_pump, args=(client, remote), daemon=True)
    worker.start()
    _pump(remote, client)
    worker.join(timeout=5)


class SplitProxyHandler(BaseHTTPRequestHandler):
    """One HTTP proxy request: ``CONNECT`` for https, absolute-URI for plain http."""

    protocol_version = "HTTP/1.0"
    server_version = "nanomuse-split/1.0"
    # unbuffered: a buffered reader would swallow the bytes that follow the CONNECT
    # request (Node sends the TLS ClientHello straight away) and the tunnel would start
    # in the middle of the handshake.
    rbufsize = 0

    @property
    def workspace(self) -> Path | None:
        return getattr(self.server, "workspace", None)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("net_proxy: " + fmt, *args)

    # ------------------------------------------------------------------ helpers
    def _cfg(self) -> dict[str, Any]:
        return load_config(self.workspace)

    def _upstream_socket(self, cfg: dict[str, Any]) -> socket.socket | None:
        upstream = parse_upstream(str(cfg.get("upstream") or ""))
        if not upstream[1]:
            return None
        kind, host, port, _user, _password = upstream
        if kind == "socks5":
            # for a plain GET the target is known, for CONNECT it is not yet
            return ("socks5", upstream)
        sock = socket.create_connection((host, port), timeout=20)
        sock.settimeout(30)
        return sock

    def _fail(self, message: str) -> None:
        try:
            self.send_error(502, message)
        except OSError:
            pass

    # ------------------------------------------------------------------ CONNECT
    def do_CONNECT(self) -> None:  # noqa: N802
        host, _, port_text = self.path.partition(":")
        port = int(port_text or 443)
        cfg = self._cfg()
        mode = route_for(host, cfg)
        sock = None
        try:
            if mode == "proxy":
                upstream = self._upstream_socket(cfg)
                if upstream is None:
                    self._fail("no upstream proxy configured")
                    return
                if isinstance(upstream, tuple):
                    sock = socks5_connect(upstream[1], host, port)
                else:
                    sock = upstream
                    sock.sendall(
                        f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
                    )
                    head = _read_headers(sock)
                    status = head.split(b"\r\n")[0] if head else b""
                    if b" 200" not in status:
                        self._fail(f"upstream refused: {status.decode('latin1', 'replace')[:80]}")
                        return
            else:
                sock = socket.create_connection((host, port), timeout=20)
                sock.settimeout(30)
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
            _tunnel(self.connection, sock)
        except Exception as exc:  # noqa: BLE001
            logger.warning("net_proxy: CONNECT {}:{} failed: {}", host, port, exc)
            self._fail(str(exc))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    # ------------------------------------------------------------------ plain http
    def _forward(self, method: str) -> None:
        url = self.path
        if not url.startswith("http://"):
            self._fail("only absolute http:// URIs are proxied")
            return
        rest = url[len("http://") :]
        hostport, _, path = rest.partition("/")
        host, _, port_text = hostport.partition(":")
        port = int(port_text or 80)
        cfg = self._cfg()
        mode = route_for(host, cfg)

        length = int(self.headers.get("Content-Length") or 0)
        body = _read_exact(self.rfile, length) if length else b""
        headers = [
            f"{method} {'/' + path if path else '/'} HTTP/1.1",
            f"Host: {hostport}",
            "Connection: close",
        ]
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP or key.lower() == "host":
                continue
            headers.append(f"{key}: {value}")

        sock = None
        try:
            if mode == "proxy":
                upstream = self._upstream_socket(cfg)
                if upstream is None:
                    self._fail("no upstream proxy configured")
                    return
                if isinstance(upstream, tuple):
                    # a SOCKS5 upstream: open the tunnel to the target first
                    sock = socks5_connect(upstream[1], host, port)
                    request_line = f"{method} {url} HTTP/1.1"
                    head = "\r\n".join([request_line, *headers[1:]]) + "\r\n\r\n"
                else:
                    sock = upstream
                    request_line = f"{method} {url} HTTP/1.1"
                    head = "\r\n".join([request_line, *headers[1:]]) + "\r\n\r\n"
            else:
                sock = socket.create_connection((host, port), timeout=20)
                sock.settimeout(30)
                head = "\r\n".join(headers) + "\r\n\r\n"
            sock.sendall(head.encode() + body)
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                self.wfile.write(data)
            self.wfile.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("net_proxy: {} {} failed: {}", method, url, exc)
            self._fail(str(exc))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def do_GET(self) -> None:  # noqa: N802
        self._forward("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._forward("POST")

    def do_HEAD(self) -> None:  # noqa: N802
        self._forward("HEAD")

    def do_PUT(self) -> None:  # noqa: N802
        self._forward("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._forward("PATCH")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._forward("OPTIONS")


# ---------------------------------------------------------------------- starting it
def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def start_server(workspace: Path | None = None) -> str | None:
    """Start the split proxy in this process; return its URL, or None if it cannot run."""
    global _state
    cfg = load_config(workspace)
    listen_host, _, listen_port = str(cfg.get("listen") or DEFAULT_LISTEN).partition(":")
    port = int(listen_port or 18080)

    if _state.get("server") is not None and _state.get("url"):
        return str(_state["url"])
    if _port_open(listen_host, port):
        url = f"http://{listen_host}:{port}"
        _state.update({"server": None, "url": url, "workspace": workspace})
        logger.info("net_proxy: reusing the split proxy already listening at {}", url)
        return url

    try:
        httpd = ThreadingHTTPServer((listen_host, port), SplitProxyHandler)
    except OSError as exc:
        logger.warning("net_proxy: cannot bind {}:{}: {}", listen_host, port, exc)
        if _port_open(listen_host, port):
            url = f"http://{listen_host}:{port}"
            _state.update({"server": None, "url": url, "workspace": workspace})
            return url
        return None
    httpd.workspace = workspace  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://{listen_host}:{port}"
    _state.update({"server": httpd, "url": url, "workspace": workspace})
    logger.info("net_proxy: split proxy listening at {} (upstream={})", url, cfg.get("upstream"))
    return url


def playwright_proxy(workspace: Path | None = None) -> dict[str, str] | None:
    """The ``proxy=`` argument for Playwright, or None when no split proxy is wanted."""
    url = start_server(workspace)
    if not url:
        return None
    return {"server": url}
