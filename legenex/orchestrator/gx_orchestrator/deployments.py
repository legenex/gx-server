"""Which deployment is ACTIVE, and the lifecycle that drives it (D-053).

The registry lists the deployments (V4 Flash primary, V4.1 rollback); which
one is serving is runtime state, persisted in
``$GX_STATE_ROOT/orchestrator/active-deployment.json`` and written ONLY by a
successful typed switch (switching.py). With no file on record the registry's
``initial_active_deployment`` applies, so deploying this code never switches
the model by itself.

`DeploymentManager` owns exactly one `GxMaxLifecycle` at a time. `LifecycleProxy`
lets the HTTP handler keep calling ``self.lifecycle.<method>`` while a switch
swaps the object underneath it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .profiles import DeploymentSpec, ProfileSpec, Registry, RegistryError

log = logging.getLogger("gx.deployments")


class ActiveDeploymentStore:
    """Atomic JSON pointer to the active deployment."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> "dict[str, Any] | None":
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("deployment"), str) else None

    def write(self, deployment: str, *, switch_id: str = "", actor: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"deployment": deployment, "since": time.time(),
                                   "switch_id": switch_id, "actor": actor}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def fixed_profile_for(dep: DeploymentSpec) -> "ProfileSpec | None":
    """A fixed-shape deployment (engine_shape) never cycles for a profile."""
    shape = dep.engine_shape
    if not shape:
        return None
    return ProfileSpec(
        name=f"{dep.name}-resident",
        max_num_seqs=int(shape.get("max_num_seqs") or 1),
        spec_method="dspark",
        max_model_len=int(shape.get("max_model_len") or 131072),
        reasoning_default="none",
        target="fixed engine shape (D-053): logical modes shape requests, never the engine",
        dspark_tokens=int(shape.get("dspark_tokens") or 0),
    )


class DeploymentManager:
    """Holds the active deployment name and its lifecycle."""

    def __init__(self, registry: Registry, store: ActiveDeploymentStore,
                 lifecycle_factory: Callable[[DeploymentSpec], Any]) -> None:
        self._registry = registry
        self._store = store
        self._factory = lifecycle_factory
        self._lock = threading.RLock()
        recorded = store.read()
        name = recorded["deployment"] if recorded else registry.initial_active_deployment
        if name not in registry.deployments:
            log.error("active-deployment file names %r, which the registry does not know; using %s",
                      name, registry.initial_active_deployment)
            name = registry.initial_active_deployment
        self._active = name
        self._lifecycle = lifecycle_factory(registry.deployment(name))

    @property
    def active_name(self) -> str:
        with self._lock:
            return self._active

    @property
    def active(self) -> DeploymentSpec:
        return self._registry.deployment(self.active_name)

    @property
    def lifecycle(self) -> Any:
        with self._lock:
            return self._lifecycle

    def activate(self, name: str) -> Any:
        """Swap to `name`'s lifecycle (the old one must already be released)."""
        dep = self._registry.deployment(name)
        with self._lock:
            old = self._lifecycle
            self._lifecycle = self._factory(dep)
            self._active = name
        try:
            old.shutdown()
        except Exception:  # noqa: BLE001 - the old reaper thread just stops
            log.debug("old lifecycle shutdown failed", exc_info=True)
        return self._lifecycle

    def persist(self, name: str, *, switch_id: str = "", actor: str = "") -> None:
        if name not in self._registry.deployments:
            raise RegistryError(f"unknown deployment {name!r}")
        self._store.write(name, switch_id=switch_id, actor=actor)


class LifecycleProxy:
    """Attribute access forwarded to the manager's CURRENT lifecycle."""

    def __init__(self, manager: DeploymentManager) -> None:
        object.__setattr__(self, "_manager", manager)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager.lifecycle, name)
