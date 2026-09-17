"""gx-live supervisor (gx10-02): the only ingress to the MiniCPM-o 4.5 live engine.

Stdlib only. Owns authentication, session admission, the realtime relay
between the tunnelled client WebSocket and the engine, the tool-call bridge
to the Control Center, engine lifecycle (admission guard, idle unload, pins,
Maintenance, gx-max) and the D-038 pending-memory contract.
"""

__version__ = "1.0.0"
PROTOCOL = "gx-live.v1"
