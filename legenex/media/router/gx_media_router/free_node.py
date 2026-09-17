"""Ask the running router to hand node 2 over (used by gx-reason's start).

    docker exec gx-media-router python -m gx_media_router.free_node

Posts to the router's own loopback with its own key, so no credential leaves
the container. Frees ComfyUI's models only when no generation is running (the
router refuses otherwise). Always exits 0: a refusal must not block gx-reason,
whose own memory check then decides.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


def main() -> int:
    port = os.environ.get("GX_MEDIA_PORT", "18800")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/admin/free", data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {os.environ.get('GX_MEDIA_API_KEY', '')}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            body = json.load(res)
    except urllib.error.HTTPError as exc:
        body = {"freed": False, "reason": f"HTTP {exc.code}"}
    except (OSError, ValueError) as exc:
        body = {"freed": False, "reason": str(exc)}
    print(json.dumps(body))
    if body.get("freed") and body.get("models"):
        time.sleep(5)  # ComfyUI applies the free flag between prompts; let it land
    return 0


if __name__ == "__main__":
    sys.exit(main())
