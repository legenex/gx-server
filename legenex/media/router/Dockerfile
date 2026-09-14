# =============================================================================
# gx-media-router -- the sole network ingress for gx-image / gx-video.
#
# Deliberately dependency-free: Python standard library only. Nothing to pin,
# nothing to audit, no aarch64 wheel availability risk, and a build that takes
# seconds so the security-critical component is never the slow one to patch.
# =============================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GX_MEDIA_WORKFLOW_DIR=/opt/router/workflows

WORKDIR /opt/router
COPY gx_media_router/ /opt/router/gx_media_router/

# Unprivileged. The router reads nothing from disk except its own workflow
# templates (mounted read-only) and never writes.
RUN useradd -r -u 10800 -s /usr/sbin/nologin router
USER 10800

EXPOSE 18800

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:18800/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "gx_media_router"]
