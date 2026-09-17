"""gx-voice: the node-2 speech service (Qwen3-TTS 12Hz 1.7B).

Two processes, the same split as gx-music:

* ``gx-voice`` supervisor (this package): stdlib-only HTTP service, the ONLY
  ingress, bound to the fabric address and loopback. It owns validation,
  the variant router, the job queue, the job/reference store, the engine
  lifecycle and the node-2 admission guard.
* ``gx-voice-engine`` container (``engine/``): the Qwen3-TTS model objects
  behind a loopback-only HTTP server. Started on demand, stopped after
  inactivity or when gx-max or Maintenance claims node 2.
"""

__version__ = "1.0.0"
