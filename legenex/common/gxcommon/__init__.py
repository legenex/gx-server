"""Shared stdlib-only helpers for GX cluster services (Build V3, PLT).

* ``gxcommon.metrics``        structured metric lines (JSON), privacy-safe
* ``gxcommon.node2_tenants``  the D-038 pending-memory contract of the node-2 tenants
* ``gxcommon.rtws``           minimal RFC 6455 WebSocket server/client endpoints (LIV; shared with CAL)

Consumers put ``<repo>/legenex/common`` on ``sys.path``. Nothing here needs a
third-party package, a secret or root.
"""

__version__ = "1.0.0"
