"""gx-call: the node-2 realtime voice-agent service (NVIDIA NemotronLabs VoiceChat 11B).

Two processes, same split as gx-music:

* ``gx-call`` supervisor (this package): stdlib-only HTTP/WebSocket service,
  the ONLY ingress, bound to the fabric address and loopback. Owns session
  admission, the one-call-at-a-time queue, the caller relay, the event log the
  Control Center persists, tool-call plumbing, optional recordings, the engine
  lifecycle and the node-2 admission guard.
* ``gx-call-engine`` container (``engine/``): NeMo's streaming speech-to-speech
  pipeline behind a loopback WebSocket. Started on demand, stopped after
  inactivity or when gx-max drains node 2. Never exposed.
"""

__version__ = "1.0.0"
