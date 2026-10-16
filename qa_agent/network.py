"""Per-run HTTP/CONNECT proxy. Resolve once, validate addresses, connect numerically.

This is application-level egress containment, not an OS sandbox. A deployment must
also deny worker direct egress; Chromium is explicitly configured to use this proxy,
including loopback. HTTPS remains end-to-end and its destination is checked at CONNECT.
"""
from __future__ import annotations

import http.client
import ipaddress
import selectors
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

from .gateway import PolicyDenied

HOP_HEADERS = {"connection", "proxy-connection", "proxy-authorization", "proxy-authenticate",
               "keep-alive", "transfer-encoding", "te", "trailer", "upgrade"}


class EgressProxy:
    def __init__(self, gateway, run_id, env, plan_hash, *, timeout=60, max_connections=32,
                 max_body_bytes=20_000_000, resolver=socket.getaddrinfo):
        self.gw, self.run_id, self.env = gateway, run_id, env
        self.ctx = {"env": env, "plan_hash": plan_hash}
        self.timeout, self.max_body_bytes, self.resolver = timeout, max_body_bytes, resolver
        self.slots = threading.BoundedSemaphore(max_connections)
        self.denials = []
        self._lock = threading.Lock()
        self._sockets = set()
        self.server = self.thread = None

    def deny(self, url, reason):
        safe = urlsplit(url)
        args = {"url": urlunsplit((safe.scheme, safe.netloc, safe.path, "", ""))}
        self.gw.record(self.run_id, "execution.network", args, "deny", reason)
        with self._lock:
            self.denials.append(reason)
        raise PolicyDenied(reason)

    def destination(self, url, method):
        u = urlsplit(url)
        if u.username or u.password or u.fragment or u.scheme not in ("http", "https"):
            self.deny(url, "invalid proxy destination")
        args = {"url": urlunsplit((u.scheme, u.netloc, u.path, "", "")), "method": method}
        reason = self.gw.check(self.run_id, "execution.network", args, self.ctx)
        if reason:
            self.deny(url, reason)
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        allowed_cidrs = [ipaddress.ip_network(c) for c in self.env.get("allowed_cidrs", [])]
        from .environments import _is_loopback
        infos = self.resolver(host, port, type=socket.SOCK_STREAM)
        if not infos:
            self.deny(url, "destination has no resolved addresses")
        addresses = []
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if getattr(ip, "ipv4_mapped", None):
                ip = ip.ipv4_mapped
            # Local synthetic targets are explicit exceptions; public hostnames may
            # not resolve into internal/metadata ranges without approved CIDRs.
            if not ip.is_global and not (_is_loopback(host) and ip.is_loopback) \
                    and not any(ip in cidr for cidr in allowed_cidrs):
                self.deny(url, "resolved address is outside approved network ranges")
            if ip.is_multicast or ip.is_unspecified or ip.is_link_local:
                self.deny(url, "multicast/unspecified/link-local destinations are prohibited")
            addresses.append(info[4])
        return host, port, addresses

    def connect(self, url, method):
        host, port, addresses = self.destination(url, method)
        last = None
        for address in addresses:
            try:
                sock = socket.create_connection((address[0], address[1]), timeout=self.timeout)
                with self._lock:
                    self._sockets.add(sock)
                self.gw.record(self.run_id, "execution.network",
                               {"host": host, "port": port, "method": method}, "allow", "validated destination")
                return sock
            except OSError as exc:
                last = exc
        raise OSError("approved destination unavailable") from last

    def discard(self, sock):
        with self._lock:
            self._sockets.discard(sock)
        sock.close()

    def __enter__(self):
        proxy = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                self._handle(tunnel=True)

            def _handle(self, tunnel=False):
                upstream = None
                if not proxy.slots.acquire(blocking=False):
                    self.send_error(503, "execution proxy capacity exceeded")
                    return
                try:
                    self.connection.settimeout(proxy.timeout)
                    if tunnel:
                        # CONNECT authority must contain no path, credentials or query.
                        u = urlsplit("https://" + self.path)
                        if u.path or u.query or u.fragment or u.username or u.password:
                            proxy.deny("https://invalid/", "invalid CONNECT authority")
                        # Playwright's APIRequestContext also tunnels plain HTTP.
                        # The approved environment fixes scheme and port; authority
                        # cannot select any other target through this tunnel.
                        scheme = urlsplit(proxy.env["base_url"]).scheme
                        upstream = proxy.connect(scheme + "://" + self.path, "CONNECT")
                        self.send_response(200, "Connection established")
                        self.end_headers()
                        self._relay(upstream)
                        return
                    u = urlsplit(self.path)
                    if u.scheme != "http" or not u.netloc:
                        proxy.deny("http://invalid/", "proxy requires an absolute HTTP URL")
                    if self.headers.get("Transfer-Encoding"):
                        proxy.deny(self.path, "chunked proxy request bodies are unsupported")
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0 or length > proxy.max_body_bytes:
                        proxy.deny(self.path, "request body exceeds execution limit")
                    upstream = proxy.connect(self.path, self.command)
                    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=proxy.timeout)
                    conn.sock = upstream
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS | {"host"}}
                    headers["Host"], headers["Connection"] = u.netloc, "close"
                    target = urlunsplit(("", "", u.path or "/", u.query, ""))
                    body = self.rfile.read(length) if length else None
                    if length and len(body) != length:
                        raise ValueError("incomplete request body")
                    conn.request(self.command, target, body=body, headers=headers)
                    response = conn.getresponse()
                    self.send_response(response.status)
                    for k, v in response.getheaders():
                        if k.lower() not in HOP_HEADERS:
                            self.send_header(k, v)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    total = 0
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > proxy.max_body_bytes:
                            proxy.deny(self.path, "response body exceeds execution limit")
                        self.wfile.write(chunk)
                except PolicyDenied:
                    # A denied hop never opens a destination socket.
                    if upstream is None:
                        self.send_error(403, "execution destination refused")
                except (OSError, ValueError, http.client.HTTPException):
                    if upstream is None:
                        self.send_error(502, "execution transport failed")
                finally:
                    self.close_connection = True
                    if upstream is not None:
                        proxy.discard(upstream)
                    proxy.slots.release()

            def _relay(self, upstream):
                # Idle deadline and fixed-size buffers bound CONNECT relay memory.
                with selectors.DefaultSelector() as sel:
                    sel.register(self.connection, selectors.EVENT_READ, upstream)
                    sel.register(upstream, selectors.EVENT_READ, self.connection)
                    while True:
                        ready = sel.select(proxy.timeout)
                        if not ready:
                            return
                        for key, _ in ready:
                            data = key.fileobj.recv(64 * 1024)
                            if not data:
                                return
                            key.data.sendall(data)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        with self._lock:
            sockets = tuple(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.discard(sock)
        self.thread.join(timeout=5)
