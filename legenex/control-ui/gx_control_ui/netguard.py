"""Outbound HTTP for user-supplied URLs, with SSRF defences (D-040).

Creative Flows (Webhook / API Request nodes), music reference analysis and
Call Agent tools all fetch URLs a user typed. Those requests must never reach:

* loopback, link-local, private (RFC 1918), CGNAT 100.64/10 (Tailscale),
  multicast, reserved or unspecified addresses, IPv6 ULA / link-local;
* the ConnectX/RoCE fabric (192.168.100.0/24, 192.168.101.0/24), which is
  inside RFC 1918 anyway and is named here for clarity;
* cloud metadata endpoints (169.254.169.254, fd00:ec2::254, ...);
* this cluster's own services by name (gx10-01, gx10-02, *.ts.net, ...).

How: the host is resolved ONCE, every resolved address is checked, and the
connection is made to that checked address (the Host header and TLS SNI keep
the original name), so DNS rebinding between check and connect cannot
redirect the request. Redirects are followed manually and each hop is
re-validated. Bodies are size-capped and every request has a timeout.

Trusted internal integrations (the cluster's own gateway, for example) never
go through this module; they use fixed, configured URLs in their own code.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
from dataclasses import dataclass, field

MAX_BODY_DEFAULT = 8 * 1024 * 1024
MAX_REDIRECTS = 3
BLOCKED_HOST_SUFFIXES = (".ts.net", ".local", ".localhost", ".internal", ".lan", ".home.arpa")
BLOCKED_HOSTS = frozenset({"localhost", "gx10-01", "gx10-02", "metadata", "metadata.google.internal",
                           "instance-data", "host.docker.internal"})
EXTRA_BLOCKED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "100.64.0.0/10",        # CGNAT / Tailscale
    "192.168.100.0/24",     # RoCE fabric A
    "192.168.101.0/24",     # RoCE fabric B
    "172.16.0.0/12",        # docker bridges
    "198.18.0.0/15",        # benchmarking
    "fd00:ec2::/32",        # AWS IPv6 metadata
    "64:ff9b::/96",         # NAT64 can map to private IPv4
))
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})


class BlockedURL(ValueError):
    """The URL points somewhere a user-supplied request may not go."""


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)

    def text(self) -> str:
        charset = "utf-8"
        ctype = self.headers.get("content-type", "")
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")


def ip_blocked(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or not ip.is_global):
        return True
    return any(ip in net for net in EXTRA_BLOCKED_NETS if net.version == ip.version)


def check_url(url: str, *, allow_http: bool = True) -> tuple[urllib.parse.SplitResult, int]:
    """Syntactic checks only (no DNS). Returns the parsed URL and its port."""
    if not isinstance(url, str) or len(url) > 2048 or any(c in url for c in "\r\n\t\x00 "):
        raise BlockedURL("URL must be a single line of at most 2048 characters")
    parts = urllib.parse.urlsplit(url)
    schemes = ("https", "http") if allow_http else ("https",)
    if parts.scheme not in schemes:
        raise BlockedURL(f"only {' and '.join(schemes)} URLs are allowed")
    if parts.username or parts.password or "@" in parts.netloc:
        raise BlockedURL("credentials in URLs are not allowed; use the integration's secret settings")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedURL("URL has no host")
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_HOST_SUFFIXES) or "." not in host and not _is_ip(host):
        raise BlockedURL(f"host {host!r} is an internal name")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise BlockedURL("invalid port") from None
    if port not in ALLOWED_PORTS:
        raise BlockedURL(f"port {port} is not allowed (use 80, 443, 8080 or 8443)")
    if _is_ip(host) and ip_blocked(host):
        raise BlockedURL("private, loopback, link-local and cluster addresses are not allowed")
    return parts, port


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def resolve_public(host: str, port: int) -> str:
    """Resolve ``host`` and return one address, refusing if ANY address is internal."""
    try:
        infos = socket.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedURL(f"cannot resolve {host}: {exc.strerror}") from None
    addrs = sorted({str(info[4][0]) for info in infos})
    if not addrs:
        raise BlockedURL(f"{host} has no addresses")
    bad = [a for a in addrs if ip_blocked(a)]
    if bad:
        raise BlockedURL(f"{host} resolves to an internal address")
    v4 = [a for a in addrs if ":" not in a]
    return (v4 or addrs)[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


def fetch(url: str, *, method: str = "GET", body: bytes | None = None, headers: dict[str, str] | None = None,
          timeout: float = 15.0, max_bytes: int = MAX_BODY_DEFAULT, allow_http: bool = True,
          resolver=resolve_public) -> FetchResult:
    """Fetch a user-supplied URL safely. Raises BlockedURL or OSError."""
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        raise BlockedURL("unsupported method")
    hops: list[str] = []
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts, port = check_url(current, allow_http=allow_http)
        host = parts.hostname or ""
        ip = resolver(host, port)
        cls = _PinnedHTTPS if parts.scheme == "https" else _PinnedHTTP
        conn = cls(host, ip, port, timeout)
        path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        send_headers = {"User-Agent": "gx-cluster/1 (+self-hosted)", "Accept-Encoding": "identity"}
        send_headers.update({k: v for k, v in (headers or {}).items()
                             if k.lower() not in ("host", "content-length", "transfer-encoding")})
        try:
            conn.request(method, path, body=body, headers=send_headers)
            resp = conn.getresponse()
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            if status in (301, 302, 303, 307, 308) and resp_headers.get("location"):
                resp.read(65536)
                hops.append(current)
                current = urllib.parse.urljoin(current, resp_headers["location"])
                if status == 303 or (status in (301, 302) and method == "POST"):
                    method, body = "GET", None
                continue
            data = resp.read(max_bytes + 1)
        finally:
            conn.close()
        truncated = len(data) > max_bytes
        return FetchResult(current, status, resp_headers, data[:max_bytes], truncated, hops)
    raise BlockedURL(f"more than {MAX_REDIRECTS} redirects")
