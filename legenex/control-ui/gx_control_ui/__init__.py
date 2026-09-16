"""gx-control-ui: the management web interface for the two-node gx-cluster.

This package is an INTERFACE to the existing control plane (LiteLLM, the
gx-orchestrator, both llama-swap instances, the node-2 media router, host
health and Git sync). It never schedules, admits or launches a model on its
own: every state change maps to one explicitly listed operation in
`actions.py`, and gx-max changes always go through the orchestrator's
sanctioned acquire/release lifecycle.

Stdlib only, like the orchestrator (D-003): it sits next to the recovery path
and must not depend on pip, a venv or a container registry.
"""

__version__ = "1.0.0"
