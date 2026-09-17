"""gx-media-router: the sole ingress in front of ComfyUI on gx10-02.

ComfyUI is NOT safe to expose. ``POST /prompt`` executes an arbitrary node
graph and ``GET /view`` is an unauthenticated file-read primitive. This package
is the only thing that listens on a routable address; it accepts a small,
validated, OpenAI-shaped request and constructs the ComfyUI graph itself from a
vetted template. A caller never supplies graph structure, node classes, model
filenames or output paths.
"""

__version__ = "2.1.0"
