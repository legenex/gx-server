"""Runtime configuration, resolved once from the environment.

No secret is stored in this module. Upstream credentials (LiteLLM master key,
orchestrator API key) are read from the process environment at call time --
the systemd unit loads them from the ignored `legenex/gateway/.env`, the same
file the orchestrator uses, so no second copy of any secret exists.

V4.1 (DeepSeek V4.1 Flash rebuild, 2026-09-27): the media / music / voice /
call / live / playground upstreams are PERMANENTLY RETIRED and their config
entries are gone. The cluster is one model (DeepSeek V4.1 Flash EXL3, served
by the Mia runtime through the orchestrator) with aliases gx-max and gx-auto.
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


#: Allowed roots for the file manager (ARCHITECTURE-V41.md section 7).
#: STRICTLY this list: every target is resolved + realpath'd against it.
FILE_ALLOWED_ROOTS: tuple[str, ...] = (
    "/home/legenex/Documents/Projects",
    "/home/legenex/Documents/Backups",
    "/home/legenex/Documents/Archive",
    "/srv/models",
    "/srv/cache",
    "/srv/logs",
)

#: Never deletable, never trashable, never purgeable: the protected backups
#: and the gx-backup repository (mission brief "Protected" list). Paths inside
#: the allowed roots that match these are read-only in the file manager.
FILE_PROTECTED_PATHS: tuple[str, ...] = (
    "/home/legenex/Documents/Backups/GX",
    "/home/legenex/Documents/Projects/gx-backup",
)

#: System prefixes the file manager never touches, even inside allowed roots.
FILE_FORBIDDEN_PREFIXES: tuple[str, ...] = ("/", "/etc", "/boot", "/usr", "/bin", "/sbin", "/var", "/proc", "/sys", "/dev", "/run")


def log_sources(repo_root: Path, srv_logs: Path, state_root: Path) -> tuple[dict, ...]:
    """The read-only log stream inventory (config-driven, no per-feature code).

    Each source: {id, label, node, kind, target, group}. `kind` is one of
    file | glob | docker | journal. Sources whose files do not exist yet
    (scheduler history, mia / dsv41 logs) appear with an honest "log file
    does not exist (yet)" error until the writer creates them.
    """
    return (
        {"id": "orchestrator", "label": "gx-orchestrator", "node": "node1", "kind": "file",
         "target": str(srv_logs / "gx-orchestrator.log"), "group": "Control plane"},
        {"id": "scheduler-history", "label": "Scheduler request history (JSONL)", "node": "node1", "kind": "file",
         "target": str(state_root / "scheduler" / "history.jsonl"), "group": "Scheduler"},
        {"id": "scheduler-queue", "label": "Scheduler queue state", "node": "node1", "kind": "file",
         "target": str(state_root / "scheduler" / "queue.json"), "group": "Scheduler"},
        {"id": "lifecycle", "label": "gx-max lifecycle events", "node": "node1", "kind": "file",
         "target": str(srv_logs / "gx-max-lifecycle.log"), "group": "gx-max"},
        {"id": "rank0", "label": "gx-max rank 0 (gx10-01)", "node": "node1", "kind": "file",
         "target": str(srv_logs / "gx-max-rank0.log"), "group": "gx-max"},
        {"id": "rank1", "label": "gx-max rank 1 (gx10-02)", "node": "node2", "kind": "file",
         "target": "/home/legenex-02/gx-max-rank1.log", "group": "gx-max"},
        {"id": "mia", "label": "Mia runtime (dsv41, latest)", "node": "node1", "kind": "glob",
         "target": str(srv_logs / "dsv41-*.log"), "group": "gx-max"},
        {"id": "litellm", "label": "LiteLLM gateway", "node": "node1", "kind": "docker",
         "target": "gx-litellm", "group": "Control plane"},
        {"id": "open-webui", "label": "OpenWebUI (user app, unmanaged)", "node": "node1", "kind": "docker",
         "target": "open-webui", "group": "Apps"},
        {"id": "backup", "label": "GX backup", "node": "node1", "kind": "journal",
         "target": "gx-backup.service", "group": "Backup"},
        {"id": "git-autosync", "label": "Git autosync (gx10-01)", "node": "node1", "kind": "file",
         "target": str(srv_logs / "gx-git-sync" / "node1-autosync.log"), "group": "Git"},
        {"id": "git-push-failures", "label": "Git push failures (gx10-01)", "node": "node1", "kind": "file",
         "target": str(srv_logs / "gx-git-sync" / "push-failures.log"), "group": "Git"},
        {"id": "git-reconcile", "label": "Git reconcile (gx10-02)", "node": "node2", "kind": "file",
         "target": str(srv_logs / "gx-git-sync" / "node2-reconcile.log"), "group": "Git"},
        {"id": "audit-node1", "label": "Daily integrity audit (gx10-01)", "node": "node1", "kind": "file",
         "target": str(srv_logs / "gx-git-sync" / "audit-latest.log"), "group": "Git"},
        {"id": "audit-node2", "label": "Daily integrity audit (gx10-02)", "node": "node2", "kind": "file",
         "target": str(srv_logs / "gx-git-sync" / "audit-latest.log"), "group": "Git"},
        {"id": "hostwatch-node1", "label": "hostwatch (gx10-01)", "node": "node1", "kind": "file",
         "target": str(srv_logs / "gx-hostwatch.log"), "group": "Host"},
        {"id": "hostwatch-node2", "label": "hostwatch (gx10-02)", "node": "node2", "kind": "file",
         "target": str(srv_logs / "gx-hostwatch.log"), "group": "Host"},
        {"id": "control-ui", "label": "control UI service", "node": "node1", "kind": "file",
         "target": str(srv_logs / "gx-control-ui" / "control-ui.log"), "group": "Control plane"},
        {"id": "control-ui-audit", "label": "control UI audit trail", "node": "node1", "kind": "file",
         "target": str(srv_logs / "gx-control-ui" / "audit.log"), "group": "Control plane"},
    )


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
    log_dir: Path = field(
        default_factory=lambda: Path(_env("GX_UI_LOG_DIR", "/srv/logs/gx-control-ui"))
    )

    #: Shared runtime state written by the orchestrator / lifecycle.
    guard_dir: Path = field(
        default_factory=lambda: Path(_env("GX_GUARD_STATE_DIR", "/srv/projects/gx-cluster/state/guard"))
    )
    gx_state_root: Path = field(
        default_factory=lambda: Path(_env("GX_STATE_ROOT", "/srv/projects/gx-cluster/state"))
    )
    srv_logs: Path = field(default_factory=lambda: Path(_env("GX_LOG_DIR", "/srv/logs")))
    #: Hugging Face read token for revision checks (0600, outside Git).
    hf_token_file: Path = field(
        default_factory=lambda: Path(_env("GX_HF_TOKEN_FILE", "/srv/projects/gx-cluster/secrets/hf/token"))
    )
    #: Where projects live (Projects / Tasks pages scan this, depth 2).
    projects_root: Path = field(
        default_factory=lambda: Path(_env("GX_PROJECTS_ROOT", "/home/legenex/Documents/Projects"))
    )
    #: Allowed roots + protected paths for the file manager (section 7).
    file_roots: tuple[str, ...] = FILE_ALLOWED_ROOTS
    file_protected: tuple[str, ...] = FILE_PROTECTED_PATHS
    trash_root: Path = field(
        default_factory=lambda: Path(_env("GX_TRASH_ROOT", "/srv/cache/trash"))
    )
    #: Watchdog incidents (written by the separate watchdog writer; read here).
    watchdog_incidents: Path = field(
        default_factory=lambda: Path(_env("GX_WATCHDOG_INCIDENTS",
                                          "/srv/projects/gx-cluster/state/watchdog/incidents.jsonl"))
    )

    # --- upstreams (all internal; the browser never talks to these) -------
    orchestrator_base: str = field(default_factory=lambda: _env("GX_UI_ORCH_BASE", "http://127.0.0.1:18900"))
    litellm_base: str = field(default_factory=lambda: _env("GX_UI_LITELLM_BASE", "http://127.0.0.1:4000"))
    #: The Mia runtime's OpenAI API (loopback only, never public). Probed for
    #: health while gx-max is READY; unreachable while it is down.
    mia_base: str = field(default_factory=lambda: _env("GX_UI_MIA_BASE", "http://127.0.0.1:8888"))
    #: AgentOS Control Center (a user app, not cluster-managed). The adapter
    #: reports {"connected": false, ...} honestly until it answers.
    agentos_base: str = field(default_factory=lambda: _env("GX_UI_AGENTOS_BASE", "http://127.0.0.1:4173"))

    #: What clients should use; shown in docs and code snippets only.
    public_gateway_url: str = field(
        default_factory=lambda: _env("GX_UI_PUBLIC_GATEWAY", "http://100.105.214.61:4000/v1")
    )
    public_control_url: str = field(
        default_factory=lambda: _env("GX_UI_PUBLIC_CONTROL", "http://100.105.214.61:8088/")
    )

    node2_ssh: str = field(default_factory=lambda: _env("GX_NODE2_SSH", "legenex-02@gx10-02"))
    node2_repo: Path = field(
        default_factory=lambda: Path(_env("GX_NODE2_REPO", "/home/legenex-02/Documents/Projects/Server/gx-cluster"))
    )
    github_url: str = field(default_factory=lambda: _env("GX_GITHUB_URL", "https://github.com/legenex/gx-server.git"))
    #: Fabric peers (fallback when the registry is not at schema 2 yet).
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
    def registry_path(self) -> Path:
        return self.repo_root / "legenex" / "models" / "registry.json"

    @property
    def mia_dir(self) -> Path:
        """The Mia runtime submodule (pins: git commit; live: rev-parse)."""
        return self.repo_root / "mia-dsv41"

    @property
    def bench_dir(self) -> Path:
        return self.repo_root / "ops" / "bench"

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
    def acceptance_password_file(self) -> Path:
        return self.secret_dir / "acceptance-password"

    def log_source_list(self) -> tuple[dict, ...]:
        """The log stream inventory for this deployment (see log_sources)."""
        return log_sources(self.repo_root, self.srv_logs, self.gx_state_root)

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
