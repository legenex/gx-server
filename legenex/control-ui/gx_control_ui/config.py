"""Runtime configuration, resolved once from the environment.

No secret is stored in this module. Upstream credentials (LiteLLM master key,
llama-swap key, media key) are read from the process environment at call time
-- the systemd unit loads them from the ignored `legenex/gateway/.env`, the
same file the orchestrator uses, so no second copy of any secret exists.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
_UI_DIR = _PKG_DIR.parent
_REPO_DEFAULT = _UI_DIR.parents[1]

#: Values that are placeholders, not credentials. Never treated as secrets to
#: redact (they would blank out ordinary words) and flagged on the System page.
PLACEHOLDER_SECRETS = frozenset({"", "CHANGEME", "not-required", "none", "changeme"})

#: Environment variables whose values must never reach a browser or a log.
SECRET_ENV_VARS = (
    "LITELLM_MASTER_KEY",
    "GX_SWAP_API_KEY",
    "GX_MEDIA_API_KEY",
    "GX_ORCHESTRATOR_API_KEY",
    "POSTGRES_PASSWORD",
    "LITELLM_UI_PASSWORD",
    "UI_PASSWORD",
    "HF_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def tailscale_ipv4() -> str | None:
    """This host's Tailscale IPv4, or None. Management plane only (L-3)."""
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
    return ip if ip.startswith("100.") else None


def _resolve_hosts(raw: str) -> tuple[str, ...]:
    hosts: list[str] = []
    for item in (h.strip() for h in raw.split(",")):
        if not item:
            continue
        if item == "tailscale":
            ts = tailscale_ipv4()
            if ts:
                hosts.append(ts)
            continue
        if item in ("0.0.0.0", "::"):
            # A management surface that can change model state is never bound
            # to every interface. Refuse rather than silently widen exposure.
            raise ValueError("GX_UI_HOSTS must not contain a wildcard address")
        hosts.append(item)
    return tuple(dict.fromkeys(hosts))


@dataclass(frozen=True)
class UIConfig:
    #: Bind addresses. Loopback plus this host's Tailscale address: reachable
    #: from the management tailnet, never from the LAN or the internet.
    hosts: tuple[str, ...] = field(
        default_factory=lambda: _resolve_hosts(_env("GX_UI_HOSTS", "127.0.0.1,tailscale"))
    )
    port: int = field(default_factory=lambda: _env_int("GX_UI_PORT", 8088))

    repo_root: Path = field(default_factory=lambda: Path(_env("GX_REPO_ROOT", str(_REPO_DEFAULT))))
    static_dir: Path = field(default_factory=lambda: Path(_env("GX_UI_STATIC_DIR", str(_UI_DIR / "web"))))
    docs_dir: Path = field(default_factory=lambda: Path(_env("GX_UI_DOCS_DIR", str(_UI_DIR / "docs"))))

    #: Mode 0700 directory holding the password store (mode 0600).
    secret_dir: Path = field(
        default_factory=lambda: Path(_env("GX_UI_SECRET_DIR", "/srv/projects/gx-cluster/secrets/control-ui"))
    )
    state_dir: Path = field(
        default_factory=lambda: Path(_env("GX_UI_STATE_DIR", "/srv/projects/gx-cluster/state/control-ui"))
    )
    log_dir: Path = field(default_factory=lambda: Path(_env("GX_UI_LOG_DIR", "/srv/logs/gx-control-ui")))

    #: Shared runtime state written by the lifecycle scripts.
    guard_dir: Path = field(
        default_factory=lambda: Path(_env("GX_GUARD_STATE_DIR", "/srv/projects/gx-cluster/state/guard"))
    )
    gx_state_root: Path = field(
        default_factory=lambda: Path(_env("GX_STATE_ROOT", "/srv/projects/gx-cluster/state"))
    )
    srv_logs: Path = field(default_factory=lambda: Path(_env("GX_LOG_DIR", "/srv/logs")))
    #: Permanent media library (D-034), outside Git.
    media_dir: Path = field(default_factory=lambda: Path(_env("GX_MEDIA_LIBRARY", "/srv/projects/gx-cluster/media")))
    #: Hugging Face read token for the Model Manager (0600, outside Git).
    hf_token_file: Path = field(
        default_factory=lambda: Path(_env("GX_HF_TOKEN_FILE", "/srv/projects/gx-cluster/secrets/hf/token"))
    )

    # --- upstreams (all internal; the browser never talks to these) -------
    orchestrator_base: str = field(default_factory=lambda: _env("GX_UI_ORCH_BASE", "http://127.0.0.1:18900"))
    litellm_base: str = field(default_factory=lambda: _env("GX_UI_LITELLM_BASE", "http://127.0.0.1:4000"))
    node1_swap_base: str = field(default_factory=lambda: _env("GX_NODE1_SWAP_BASE", "http://127.0.0.1:28080"))
    #: Fabric address, never Tailscale (L-3).
    node2_swap_base: str = field(default_factory=lambda: _env("GX_NODE2_SWAP_BASE", "http://192.168.100.11:28080"))
    media_base: str = field(default_factory=lambda: _env("GX_UI_MEDIA_BASE", "http://192.168.100.11:18800"))
    gxmax_base: str = field(default_factory=lambda: _env("GX_UI_GXMAX_BASE", "http://127.0.0.1:30000"))
    #: gx-voice supervisor on gx10-02 (fabric only, L-3) and its bearer key file (0600). Build V3 VOI.
    voice_base: str = field(default_factory=lambda: _env("GX_UI_VOICE_BASE", "http://192.168.100.11:18830"))
    voice_key_file: Path = field(
        default_factory=lambda: Path(_env("GX_VOICE_KEY_FILE", "/srv/projects/gx-cluster/secrets/gx-voice/api-key"))
    )
    #: gx-music supervisor on gx10-02 (fabric only, L-3) and its bearer key file (0600).
    music_base: str = field(default_factory=lambda: _env("GX_UI_MUSIC_BASE", "http://192.168.100.11:18820"))
    music_key_file: Path = field(
        default_factory=lambda: Path(_env("GX_MUSIC_KEY_FILE", "/srv/projects/gx-cluster/secrets/gx-music/api-key"))
    )

    #: What clients should use; shown in docs and code snippets only.
    public_gateway_url: str = field(
        default_factory=lambda: _env("GX_UI_PUBLIC_GATEWAY", "http://100.105.214.61:4000/v1")
    )

    #: GX-Playground (D-037): the creative app on port 8090 that proxies to this backend.
    public_playground_url: str = field(
        default_factory=lambda: _env("GX_UI_PUBLIC_PLAYGROUND", "http://100.105.214.61:8090/")
    )
    public_control_url: str = field(
        default_factory=lambda: _env("GX_UI_PUBLIC_CONTROL", "http://100.105.214.61:8088/")
    )

    node2_ssh: str = field(default_factory=lambda: _env("GX_NODE2_SSH", "legenex-02@gx10-02"))
    node2_repo: str = field(
        default_factory=lambda: _env("GX_NODE2_REPO", "/home/legenex-02/Documents/Projects/Server/gx-cluster")
    )
    github_url: str = field(default_factory=lambda: _env("GX_GITHUB_URL", "https://github.com/legenex/gx-server.git"))
    fabric_peers: tuple[str, ...] = ("192.168.100.11", "192.168.101.11")
    fabric_local: tuple[str, ...] = ("192.168.100.10", "192.168.101.10")

    # --- sessions --------------------------------------------------------
    session_idle_seconds: int = field(default_factory=lambda: _env_int("GX_UI_SESSION_IDLE", 3600))
    session_max_seconds: int = field(default_factory=lambda: _env_int("GX_UI_SESSION_MAX", 43200))
    #: "auto" -> Secure cookie only when the request arrived over HTTPS.
    cookie_secure: str = field(default_factory=lambda: _env("GX_UI_COOKIE_SECURE", "auto"))

    # --- caching ---------------------------------------------------------
    local_ttl: float = 4.0
    node2_ttl: float = 8.0
    service_ttl: float = 5.0
    git_remote_ttl: float = 60.0

    #: Test hook: disable anything that touches the real cluster.
    offline: bool = field(default_factory=lambda: _env("GX_UI_OFFLINE", "0") == "1")

    @property
    def lifecycle_dir(self) -> Path:
        return self.repo_root / "legenex" / "lifecycle"

    @property
    def password_file(self) -> Path:
        return self.secret_dir / "auth.json"

    @property
    def initial_password_file(self) -> Path:
        return self.secret_dir / "initial-admin-password"

    #: Optional second account for automated acceptance runs (D-035). It can
    #: only sign in from 127.0.0.1 (gx10-01 itself), never over Tailscale.
    ACCEPTANCE_USER = "acceptance"

    @property
    def acceptance_file(self) -> Path:
        return self.secret_dir / "acceptance.json"

    @property
    def proxy_token_file(self) -> Path:
        """Shared with gx-playground (same user): lets the Playground proxy pass
        the real client address. Created by this service at start (0600)."""
        return self.secret_dir / "proxy-token"

    @property
    def acceptance_password_file(self) -> Path:
        return self.secret_dir / "acceptance-password"

    # --- realtime tunnel (Build V3, plt.md section 1) --------------------
    #: node-2 WebSocket services the Playground may tunnel to. Fixed here,
    #: never chosen by a client. Fabric addresses only (L-3).
    rt_call_target: str = field(default_factory=lambda: _env("GX_RT_CALL_TARGET", "192.168.100.11:18840"))
    rt_live_target: str = field(default_factory=lambda: _env("GX_RT_LIVE_TARGET", "192.168.100.11:18850"))
    secrets_root: Path = field(
        default_factory=lambda: Path(_env("GX_SECRETS_ROOT", "/srv/projects/gx-cluster/secrets"))
    )
    #: Structured metric lines (gxcommon.metrics) written on gx10-01.
    metrics_dir: Path = field(default_factory=lambda: Path(_env("GX_METRICS_DIR", "/srv/logs/gx-metrics")))

    def realtime_targets(self) -> dict:
        from .realtime import Target
        out = {}
        for svc, raw in (("call", self.rt_call_target), ("live", self.rt_live_target)):
            if raw:
                out[svc] = Target.parse(raw, self.secrets_root / f"gx-{svc}" / "api-key")
        return out

    def secret(self, name: str) -> str | None:
        value = os.environ.get(name)
        return value if value else None


def secret_values() -> list[str]:
    """Current values of known secret variables, for exact-match redaction."""
    out = []
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name, "")
        if value and value not in PLACEHOLDER_SECRETS and len(value) >= 8:
            out.append(value)
    return sorted(set(out), key=len, reverse=True)
