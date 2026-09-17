"""gx-music: the node-2 music generation service (ACE-Step 1.5 XL).

Two processes, same split as gx-media:

* ``gx-music`` supervisor (this package): stdlib-only HTTP service, the ONLY
  ingress, bound to the fabric address and loopback. Owns validation, the job
  queue, the durable job/asset store, the engine lifecycle and the node-2
  admission guard.
* ``gx-music-engine`` container: upstream ``acestep.api_server`` bound to host
  loopback. Started on demand, stopped after inactivity or when gx-max drains
  node 2. Never exposed.
"""

__version__ = "1.0.0"
