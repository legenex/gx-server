"""Optional HTTPS listener material for GX-Playground (Build V3, plt.md section 2).

Browsers only allow the microphone and camera in a secure context, and
``http://100.105.214.61:8090`` is not one. Tailscale HTTPS certificates are
not enabled on the tailnet and the Cloudflare tunnel is out of bounds, so the
Playground serves HTTPS with a certificate from a LOCAL private CA:

    /srv/projects/gx-cluster/secrets/playground-tls/   (0700)
        ca.key      0600  private CA key (never served, never copied)
        ca.crt      0644  the certificate users trust once (served at /pg/ca.crt)
        server.key  0600
        server.crt  0644  SANs: gx10-01, gx10-01.taila7ef6a.ts.net, 100.105.214.61, 127.0.0.1

The CA carries X.509 name constraints for exactly those names, so even a
leaked CA key cannot mint a certificate a browser accepts for any other site.
Keys and certificates are made with the system ``openssl`` binary (no
third-party Python package). ``python3 -m gx_playground.tls ensure`` is
idempotent and renews the server certificate 30 days before it expires.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import ssl
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DIR = Path(os.environ.get("GX_PG_TLS_DIR", "/srv/projects/gx-cluster/secrets/playground-tls"))
DEFAULT_NAMES = ("gx10-01", "gx10-01.taila7ef6a.ts.net", "100.105.214.61", "127.0.0.1")
SERVER_DAYS = 397
CA_DAYS = 3650
RENEW_BEFORE_S = 30 * 86400


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def ca_key(self) -> Path:
        return self.root / "ca.key"

    @property
    def ca_crt(self) -> Path:
        return self.root / "ca.crt"

    @property
    def key(self) -> Path:
        return self.root / "server.key"

    @property
    def crt(self) -> Path:
        return self.root / "server.crt"

    def ready(self) -> bool:
        return all(p.is_file() for p in (self.ca_crt, self.key, self.crt))


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def san_line(names: tuple[str, ...]) -> str:
    return ",".join(f"IP:{n}" if _is_ip(n) else f"DNS:{n}" for n in names)


def constraints_line(names: tuple[str, ...]) -> str:
    parts = []
    for n in names:
        if _is_ip(n):
            ip = ipaddress.ip_address(n)
            mask = "255.255.255.255" if ip.version == 4 else "FFFF:FFFF:FFFF:FFFF:FFFF:FFFF:FFFF:FFFF"
            parts.append(f"permitted;IP:{n}/{mask}")
        else:
            parts.append(f"permitted;DNS:{n}")
    return "critical," + ",".join(parts)


def _openssl(args: list[str], cwd: Path) -> None:
    res = subprocess.run(["openssl", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"openssl {args[0]} failed: {res.stderr.strip()[-400:]}")


def _write_private(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _x509(path: Path, *args: str) -> str:
    res = subprocess.run(["openssl", "x509", "-noout", "-in", str(path), *args], capture_output=True, text=True,
                         timeout=30)
    if res.returncode != 0:
        raise ValueError(f"cannot read {path.name}: {res.stderr.strip()[-200:]}")
    return res.stdout


def cert_not_after(path: Path) -> float:
    line = _x509(path, "-enddate").strip()
    return float(ssl.cert_time_to_seconds(line.partition("=")[2]))


def cert_sans(path: Path) -> set[str]:
    out = _x509(path, "-ext", "subjectAltName")
    names: set[str] = set()
    for line in out.splitlines()[1:]:
        for item in line.split(","):
            kind, _, value = item.strip().partition(":")
            if kind in ("DNS", "IP Address") and value:
                names.add(value.strip())
    return names


def fingerprint(path: Path) -> str:
    der = ssl.PEM_cert_to_DER_cert(path.read_text(encoding="ascii"))
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def ensure(root: Path = DEFAULT_DIR, names: tuple[str, ...] = DEFAULT_NAMES, *, now: float | None = None) -> dict:
    """Create the CA and server certificate if missing, or renew the server certificate."""
    paths = Paths(root)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    actions: list[str] = []
    now = time.time() if now is None else now
    with tempfile.TemporaryDirectory(prefix="gx-pg-tls-") as tmpd:
        tmp = Path(tmpd)
        if not paths.ca_key.is_file() or not paths.ca_crt.is_file():
            (tmp / "ca.cnf").write_text(
                "[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=v3\n"
                "[dn]\nO=GX cluster (local)\nCN=GX-Playground local CA\n"
                "[v3]\nbasicConstraints=critical,CA:TRUE,pathlen:0\n"
                "keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n"
                f"nameConstraints={constraints_line(names)}\n", encoding="ascii")
            _openssl(["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ca.key"], tmp)
            _openssl(["req", "-new", "-x509", "-sha256", "-key", "ca.key", "-days", str(CA_DAYS),
                      "-config", "ca.cnf", "-out", "ca.crt"], tmp)
            _write_private(paths.ca_key, (tmp / "ca.key").read_bytes(), 0o600)
            _write_private(paths.ca_crt, (tmp / "ca.crt").read_bytes(), 0o644)
            for stale in (paths.crt, paths.key):
                stale.unlink(missing_ok=True)
            actions.append("created local CA")
        renew = not paths.crt.is_file() or not paths.key.is_file()
        if not renew:
            renew = cert_not_after(paths.crt) - now < RENEW_BEFORE_S or not set(names) <= cert_sans(paths.crt)
        if renew:
            ext = tmp / "srv.ext"
            ext.write_text("[v3]\nbasicConstraints=critical,CA:FALSE\n"
                           "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
                           f"subjectAltName={san_line(names)}\nsubjectKeyIdentifier=hash\n"
                           "authorityKeyIdentifier=keyid\n", encoding="ascii")
            (tmp / "ca.key").write_bytes(paths.ca_key.read_bytes())
            os.chmod(tmp / "ca.key", 0o600)
            (tmp / "ca.crt").write_bytes(paths.ca_crt.read_bytes())
            _openssl(["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "server.key"], tmp)
            (tmp / "srv.cnf").write_text(f"[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN={names[0]}\n",
                                         encoding="ascii")
            _openssl(["req", "-new", "-sha256", "-key", "server.key", "-config", "srv.cnf", "-out", "server.csr"],
                     tmp)
            _openssl(["x509", "-req", "-sha256", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
                      "-set_serial", str(int.from_bytes(os.urandom(8), "big")), "-days", str(SERVER_DAYS),
                      "-extfile", "srv.ext", "-extensions", "v3", "-out", "server.crt"], tmp)
            _write_private(paths.key, (tmp / "server.key").read_bytes(), 0o600)
            _write_private(paths.crt, (tmp / "server.crt").read_bytes(), 0o644)
            actions.append("issued server certificate")
    return {"dir": str(root), "actions": actions, "ca_fingerprint_sha256": fingerprint(paths.ca_crt),
            "server_not_after": cert_not_after(paths.crt), "names": list(names)}


def server_context(root: Path = DEFAULT_DIR) -> ssl.SSLContext:
    paths = Paths(root)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.options |= ssl.OP_NO_COMPRESSION | ssl.OP_CIPHER_SERVER_PREFERENCE
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")
    ctx.load_cert_chain(str(paths.crt), str(paths.key))
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def info(root: Path = DEFAULT_DIR) -> dict:
    paths = Paths(root)
    if not paths.ready():
        return {"configured": False}
    try:
        return {"configured": True, "ca_fingerprint_sha256": fingerprint(paths.ca_crt),
                "server_not_after": cert_not_after(paths.crt), "names": sorted(cert_sans(paths.crt))}
    except (OSError, ValueError, ssl.SSLError):
        return {"configured": False}


def main(argv: list[str]) -> int:
    import json
    if argv[:1] == ["ensure"]:
        print(json.dumps(ensure()))
        return 0
    if argv[:1] == ["info"]:
        print(json.dumps(info()))
        return 0
    print("usage: python3 -m gx_playground.tls ensure|info", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
