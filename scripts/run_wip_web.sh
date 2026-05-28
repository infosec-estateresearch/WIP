#!/usr/bin/env bash
# Dev launcher for wip-web. systemd uses a similar invocation; see
# systemd/wip-web.service.
set -euo pipefail

cd /home/WIP
exec /usr/local/bin/uvicorn src.wip_web.app:app \
    --host 0.0.0.0 \
    --port 8300 \
    --workers 1 \
    --log-level info
