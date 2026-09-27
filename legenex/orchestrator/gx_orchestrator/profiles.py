"""Registry v2 profile/model loading (ARCHITECTURE-V41 §2).

There is exactly ONE model now (DeepSeek V4.1 Flash EXL3, served by the Mia
kit). "Routing" means choosing a (profile, reasoning) pair, never a model.
Every fact -- profiles, reasoning levels, node addresses, runtimes, model
entries, aliases -- comes from ``legenex/models/registry.json`` (schema 2).
No model or node names are hardcoded here beyond the validation vocabulary.

Pure data plus small pure helpers: the only I/O is reading the registry file,
so this can be imported by the server, the lifecycle, the scheduler, the
status CLI and the tests alike.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

#: The registry schema this code understands. The registry is owned by the
#: Model Manager worker; if it moves to a newer schema this loader must be
#: taught about it before anything else will boot.
SUPPORTED_SCHEMA = 2

#: The two public aliases (ARCHITECTURE-V41 §1). Everything else is retired.
ALIAS_DIRECT = "gx-max"
ALIAS_AUTO = "gx-auto"


class RegistryError(RuntimeError):
    """The registry is missing, unreadable, or does not match schema 2.

    Raised loudly at load time: the orchestrator must never guess a profile
    or a model id from stale defaults.
    """


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise RegistryError(f"{where}: missing required key '{key}'")
    return mapping[key]


def _require_int(mapping: Mapping[str, Any], key: str, where: str, *, minimum: int = 1) -> int:
    value = _require(mapping, key, where)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RegistryError(f"{where}: '{key}' must be an integer >= {minimum}, got {value!r}")
    return value


def _require_str(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = _require(mapping, key, where)
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{where}: '{key}' must be a non-empty string, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Profile / reasoning / node / runtime / model specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileSpec:
    """One serving profile: the (max_num_seqs, speculation, window) triple.

    A profile is how the single model is shaped for a workload class -- it is
    NOT a model choice. `custom` is bounded: `max_num_seqs_bounds` /
    `max_model_len_bounds` carry the allowed range so callers can validate a
    client-supplied override instead of trusting it.
    """

    name: str
    max_num_seqs: int
    spec_method: str  # "dspark" | "none"
    max_model_len: int
    reasoning_default: str
    target: str = ""
    dspark_tokens: int = 0
    #: Present only on the bounded `custom` profile.
    max_num_seqs_bounds: tuple[int, int] | None = None
    max_model_len_bounds: tuple[int, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "max_num_seqs": self.max_num_seqs,
            "spec_method": self.spec_method,
            "dspark_tokens": self.dspark_tokens,
            "max_model_len": self.max_model_len,
            "reasoning_default": self.reasoning_default,
            "target": self.target,
        }


@dataclass(frozen=True)
class ReasoningSpec:
    """The reasoning vocabulary and its chat_template_kwargs mapping."""

    levels: tuple[str, ...]
    mapping: dict[str, dict[str, Any]]
    numeric_range: tuple[int, int]

    def kwargs(self, level: str | int) -> dict[str, Any]:
        """chat_template_kwargs for one reasoning level.

        `none`/`minimal` disable thinking outright; the named levels map to
        the model's reasoning_effort scale (low=50 .. max=100); a plain
        integer 1-100 is allowed for custom levels. Anything else is a
        configuration/protocol error, never a silent default.
        """
        if level is None or isinstance(level, bool):
            raise RegistryError(f"invalid reasoning level: {level!r}")
        if isinstance(level, int):
            lo, hi = self.numeric_range
            if not lo <= level <= hi:
                raise RegistryError(
                    f"reasoning level {level} outside the numeric range [{lo}, {hi}]"
                )
            return {"reasoning_effort": level}
        if not isinstance(level, str):
            raise RegistryError(f"invalid reasoning level: {level!r}")
        text = level.strip().lower()
        if text in self.mapping:
            return dict(self.mapping[text])
        # Accept a numeric string ("75") for callers that pass headers verbatim.
        if text.isdigit():
            return self.kwargs(int(text))
        raise RegistryError(
            f"unknown reasoning level {level!r}; valid: {', '.join(self.levels)} "
            f"or an integer in [{self.numeric_range[0]}, {self.numeric_range[1]}]"
        )

    def is_valid(self, level: Any) -> bool:
        try:
            self.kwargs(level)
        except RegistryError:
            return False
        return True


@dataclass(frozen=True)
class NodeSpec:
    """One cluster node, from the registry `nodes` section."""

    name: str
    role: str
    user: str
    lan_ip: str
    tailscale_ip: str
    fabric: dict[str, str]
    hcas: tuple[str, ...]
    ssh: str

    @property
    def fabric_ips(self) -> tuple[str, ...]:
        return tuple(self.fabric.values())


@dataclass(frozen=True)
class RuntimeSpec:
    """One serving runtime (the Mia kit) and how to drive it."""

    name: str
    kind: str
    submodule: str
    api: str
    served_model_id: str
    start: str
    stop: str
    status: str
    image: str = ""
    commit: str = ""


@dataclass(frozen=True)
class ModelSpec:
    """One weights entry (stock / uncensored)."""

    id: str
    source: str
    revision: str
    path: str
    engram_dir: str
    uncensored: bool
    quant: str
    vision: bool
    tools: bool
    max_context: int
    serving_notes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AliasSpec:
    name: str
    model: str
    runtime: str
    mode: str  # "direct" | "auto"
    description: str = ""


@dataclass(frozen=True)
class Registry:
    """The validated registry v2."""

    path: Path
    cluster: dict[str, Any]
    nodes: dict[str, NodeSpec]
    runtimes: dict[str, RuntimeSpec]
    models: dict[str, ModelSpec]
    aliases: dict[str, AliasSpec]
    profiles: dict[str, ProfileSpec]
    reasoning: ReasoningSpec
    capabilities: dict[str, Any]

    # ------------------------------------------------------------- accessors
    def profile(self, name: str) -> ProfileSpec:
        try:
            return self.profiles[name]
        except KeyError:
            known = ", ".join(sorted(self.profiles))
            raise RegistryError(f"unknown profile {name!r}; known profiles: {known}") from None

    def node(self, name: str) -> NodeSpec:
        try:
            return self.nodes[name]
        except KeyError:
            known = ", ".join(sorted(self.nodes))
            raise RegistryError(f"unknown node {name!r}; known nodes: {known}") from None

    def runtime(self, name: str) -> RuntimeSpec:
        try:
            return self.runtimes[name]
        except KeyError:
            known = ", ".join(sorted(self.runtimes))
            raise RegistryError(f"unknown runtime {name!r}; known runtimes: {known}") from None

    def model(self, model_id: str) -> ModelSpec:
        try:
            return self.models[model_id]
        except KeyError:
            known = ", ".join(sorted(self.models))
            raise RegistryError(f"unknown model {model_id!r}; known models: {known}") from None

    def head_node(self) -> NodeSpec:
        return self.node(str(self.cluster.get("head", "")))

    def worker_node(self) -> NodeSpec:
        return self.node(str(self.cluster.get("worker", "")))

    def production_model(self) -> ModelSpec:
        """The model the `gx-max` alias is bound to (the production choice)."""
        return self.model(self.alias(ALIAS_DIRECT).model)

    def alias(self, name: str) -> AliasSpec:
        try:
            return self.aliases[name]
        except KeyError:
            raise RegistryError(f"unknown alias {name!r}; known: {', '.join(sorted(self.aliases))}") from None

    def reasoning_kwargs(self, level: str | int) -> dict[str, Any]:
        return self.reasoning.kwargs(level)

    def default_profile(self) -> ProfileSpec:
        """The profile assumed for a direct gx-max request with no override.

        `balanced` is the AgentOS default per the registry targets; this
        lookup walks the profile table so a registry edit moves the default
        with it.
        """
        for preferred in ("balanced", "fast", "deep"):
            if preferred in self.profiles:
                return self.profiles[preferred]
        # Any registry with at least one profile reaches here only when the
        # conventional names are all absent; fall back to the first entry.
        return next(iter(self.profiles.values()))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_reasoning(raw: Mapping[str, Any]) -> ReasoningSpec:
    where = "registry.reasoning"
    levels = tuple(str(x) for x in _require(raw, "levels", where))
    mapping_raw = _require(raw, "mapping", where)
    if not isinstance(mapping_raw, Mapping):
        raise RegistryError(f"{where}: 'mapping' must be an object")
    mapping: dict[str, dict[str, Any]] = {}
    for level, kwargs in mapping_raw.items():
        if not isinstance(kwargs, Mapping):
            raise RegistryError(f"{where}: mapping for '{level}' must be an object")
        mapping[str(level)] = dict(kwargs)
    missing = [lvl for lvl in levels if lvl not in mapping]
    if missing:
        raise RegistryError(f"{where}: levels {missing} have no mapping entry")
    range_raw = _require(raw, "numeric_range", where)
    if (
        not isinstance(range_raw, list)
        or len(range_raw) != 2
        or not all(isinstance(x, int) and not isinstance(x, bool) for x in range_raw)
    ):
        raise RegistryError(f"{where}: 'numeric_range' must be [low, high] integers")
    return ReasoningSpec(levels, mapping, (range_raw[0], range_raw[1]))


def _parse_profile(name: str, raw: Mapping[str, Any]) -> ProfileSpec:
    where = f"registry.profiles.{name}"
    if "bounded" in raw and raw.get("bounded"):
        # The documented bounded `custom` profile carries only its ranges and
        # the default reasoning level; the serving-shape fields are optional
        # (absent means the kit's own defaults apply).
        seq_bounds = _require(raw, "max_num_seqs", where)
        len_bounds = _require(raw, "max_model_len", where)
        if (
            not isinstance(seq_bounds, list) or len(seq_bounds) != 2
            or not isinstance(len_bounds, list) or len(len_bounds) != 2
        ):
            raise RegistryError(f"{where}: bounded profile needs [low, high] ranges")
        return ProfileSpec(
            name=name,
            max_num_seqs=int(seq_bounds[0]),
            spec_method=str(raw.get("spec_method") or "dspark"),
            dspark_tokens=int(raw.get("dspark_tokens") or 0),
            max_model_len=int(len_bounds[0]),
            reasoning_default=str(_require(raw, "reasoning_default", where)),
            target=str(raw.get("target") or ""),
            max_num_seqs_bounds=(int(seq_bounds[0]), int(seq_bounds[1])),
            max_model_len_bounds=(int(len_bounds[0]), int(len_bounds[1])),
        )
    spec_method = _require_str(raw, "spec_method", where)
    if spec_method not in ("dspark", "none"):
        raise RegistryError(f"{where}: spec_method must be 'dspark' or 'none', got {spec_method!r}")
    # dspark_tokens is OPTIONAL: the documented `long` profile omits it and the
    # Mia kit's own default (k=3, the measured optimum) applies. 0 means that.
    dspark = int(raw.get("dspark_tokens") or 0)
    return ProfileSpec(
        name=name,
        max_num_seqs=_require_int(raw, "max_num_seqs", where),
        spec_method=spec_method,
        dspark_tokens=dspark,
        max_model_len=_require_int(raw, "max_model_len", where),
        reasoning_default=_require_str(raw, "reasoning_default", where),
        target=str(raw.get("target") or ""),
    )


def _parse_node(name: str, raw: Mapping[str, Any]) -> NodeSpec:
    where = f"registry.nodes.{name}"
    fabric_raw = _require(raw, "fabric", where)
    if not isinstance(fabric_raw, Mapping) or not fabric_raw:
        raise RegistryError(f"{where}: 'fabric' must be a non-empty object of rail -> ip")
    return NodeSpec(
        name=name,
        role=_require_str(raw, "role", where),
        user=_require_str(raw, "user", where),
        lan_ip=_require_str(raw, "lan_ip", where),
        tailscale_ip=str(raw.get("tailscale_ip") or ""),
        fabric={str(k): str(v) for k, v in fabric_raw.items()},
        hcas=tuple(str(x) for x in raw.get("hcas") or ()),
        ssh=_require_str(raw, "ssh", where),
    )


def _parse_runtime(name: str, raw: Mapping[str, Any]) -> RuntimeSpec:
    where = f"registry.runtimes.{name}"
    return RuntimeSpec(
        name=name,
        kind=_require_str(raw, "kind", where),
        submodule=_require_str(raw, "submodule", where),
        api=_require_str(raw, "api", where),
        served_model_id=_require_str(raw, "served_model_id", where),
        start=str(raw.get("start") or "./start.sh"),
        stop=str(raw.get("stop") or "./stop.sh"),
        status=str(raw.get("status") or "./start.sh status"),
        image=str(raw.get("image") or ""),
        commit=str(raw.get("commit") or ""),
    )


def _parse_model(model_id: str, raw: Mapping[str, Any]) -> ModelSpec:
    where = f"registry.models.{model_id}"
    return ModelSpec(
        id=model_id,
        source=_require_str(raw, "source", where),
        revision=str(raw.get("revision") or ""),
        path=_require_str(raw, "path", where),
        engram_dir=_require_str(raw, "engram_dir", where),
        uncensored=bool(raw.get("uncensored")),
        quant=str(raw.get("quant") or ""),
        vision=bool(raw.get("vision")),
        tools=bool(raw.get("tools")),
        max_context=_require_int(raw, "max_context", where),
        serving_notes=dict(raw.get("serving_notes") or {}),
    )


def parse_registry(data: Any, *, path: Path | None = None) -> Registry:
    """Validate a parsed registry document and build the Registry object."""
    where = "registry"
    if not isinstance(data, Mapping):
        raise RegistryError("registry root must be a JSON object")
    schema = data.get("schema")
    if schema != SUPPORTED_SCHEMA:
        raise RegistryError(
            f"registry schema is {schema!r}; this orchestrator speaks schema {SUPPORTED_SCHEMA} "
            "(ARCHITECTURE-V41 §2)"
        )

    cluster = data.get("cluster")
    if not isinstance(cluster, Mapping):
        raise RegistryError(f"{where}: 'cluster' must be an object")
    for key in ("name", "head", "worker"):
        _require_str(cluster, key, f"{where}.cluster")

    raw_nodes = _require(data, "nodes", where)
    raw_runtimes = _require(data, "runtimes", where)
    raw_models = _require(data, "models", where)
    raw_aliases = _require(data, "aliases", where)
    raw_profiles = _require(data, "profiles", where)
    raw_reasoning = _require(data, "reasoning", where)
    for section in ("nodes", "runtimes", "models", "aliases", "profiles"):
        src = {"nodes": raw_nodes, "runtimes": raw_runtimes, "models": raw_models,
               "aliases": raw_aliases, "profiles": raw_profiles}[section]
        if not isinstance(src, Mapping) or not src:
            raise RegistryError(f"{where}: '{section}' must be a non-empty object")

    nodes = {n: _parse_node(n, r) for n, r in raw_nodes.items() if isinstance(r, Mapping)}
    runtimes = {n: _parse_runtime(n, r) for n, r in raw_runtimes.items() if isinstance(r, Mapping)}
    models = {n: _parse_model(n, r) for n, r in raw_models.items() if isinstance(r, Mapping)}
    profiles = {n: _parse_profile(n, r) for n, r in raw_profiles.items() if isinstance(r, Mapping)}
    if not profiles:
        raise RegistryError(f"{where}: no usable profile entries")

    aliases: dict[str, AliasSpec] = {}
    for name, raw in raw_aliases.items():
        if not isinstance(raw, Mapping):
            continue
        a = f"{where}.aliases.{name}"
        mode = _require_str(raw, "mode", a)
        if mode not in ("direct", "auto"):
            raise RegistryError(f"{a}: mode must be 'direct' or 'auto', got {mode!r}")
        aliases[name] = AliasSpec(
            name=name,
            model=_require_str(raw, "model", a),
            runtime=_require_str(raw, "runtime", a),
            mode=mode,
            description=str(raw.get("description") or ""),
        )
    for required in (ALIAS_DIRECT, ALIAS_AUTO):
        if required not in aliases:
            raise RegistryError(f"{where}: required alias '{required}' is missing")

    # Cross-references: every alias names a real model + runtime.
    for alias in aliases.values():
        if alias.model not in models:
            raise RegistryError(
                f"{where}: alias '{alias.name}' names unknown model '{alias.model}'"
            )
        if alias.runtime not in runtimes:
            raise RegistryError(
                f"{where}: alias '{alias.name}' names unknown runtime '{alias.runtime}'"
            )

    reasoning = _parse_reasoning(raw_reasoning)
    # Every profile's default reasoning level must exist in the vocabulary.
    for profile in profiles.values():
        if not reasoning.is_valid(profile.reasoning_default):
            raise RegistryError(
                f"{where}.profiles.{profile.name}: reasoning_default "
                f"{profile.reasoning_default!r} is not a valid reasoning level"
            )

    return Registry(
        path=path or Path(""),
        cluster=dict(cluster),
        nodes=nodes,
        runtimes=runtimes,
        models=models,
        aliases=aliases,
        profiles=profiles,
        reasoning=reasoning,
        capabilities=dict(data.get("capabilities") or {}),
    )


def load_registry(path: "str | os.PathLike[str]") -> Registry:
    """Read and validate the registry file. Raises RegistryError on any problem."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"cannot read registry at {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry at {p} is not valid JSON: {exc}") from exc
    return parse_registry(data, path=p)
