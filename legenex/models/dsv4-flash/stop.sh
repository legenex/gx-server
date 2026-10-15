#!/usr/bin/env bash
# stop.sh — the sanctioned stop path of the dsv4-flash kit (`./start.sh stop`).
# Removes only this kit's containers (dsv4f-head on gx10-01, dsv4f-worker and
# the dsv4f-nfs exporter on gx10-02) and the head's NFS volume. Weights and
# caches stay on disk. `--force` is accepted (the orchestrator's unwind passes
# it) and behaves the same: there is no graceful drain at this layer.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/start.sh" stop
