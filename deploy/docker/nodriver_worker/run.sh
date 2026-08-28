#!/bin/sh
# nodriver-worker launcher.
#
# Headful mode (default) needs the Xvfb display to be up before Chromium
# launches; supervisord starts both in the same batch, so wait for the X
# socket (up to ~30s) before execing uvicorn. In headless mode (or when
# stealth is disabled) the wait is skipped.
set -eu

headless() {
    case "${NODRIVER_HEADLESS:-0}" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

if ! headless; then
    i=0
    while [ ! -e /tmp/.X11-unix/X99 ] && [ "$i" -lt 60 ]; do
        sleep 0.5
        i=$((i + 1))
    done
    if [ -e /tmp/.X11-unix/X99 ]; then
        echo "nodriver-worker: Xvfb ready (DISPLAY=${DISPLAY:-:99})"
    else
        echo "nodriver-worker: WARNING: Xvfb socket /tmp/.X11-unix/X99 not found; Chromium headful start may fail" >&2
    fi
fi

# Bind 0.0.0.0 INSIDE the container (same posture as the proven cfbridge
# sidecar on 0.0.0.0:8000): a 127.0.0.1 bind is unreachable through docker
# port mapping (-p 18001:8001) or from other containers on the network,
# since docker-proxy connects the container's bridge IP, not its loopback.
# From the host it is only reachable if the operator maps the port; the
# container image never exposes it by default (no EXPOSE to a public net).
exec /opt/nodriver-worker/bin/uvicorn worker:app \
    --host 0.0.0.0 --port 8001 --log-level info
