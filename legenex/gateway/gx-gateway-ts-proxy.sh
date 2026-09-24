#!/usr/bin/env bash
# Wrapper kept for operators; the systemd unit runs the Python proxy directly.
exec /usr/bin/python3 /home/legenex/Documents/Projects/Server/gx-cluster/legenex/gateway/gx-gateway-ts-proxy.py

