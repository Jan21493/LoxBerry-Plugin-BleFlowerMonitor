#!/bin/sh
set -eu
export PYTHONIOENCODING=utf-8

PLUGINBIN=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -x "$PLUGINBIN/venv/bin/python3" ]; then
    echo "MiFlora Python environment missing. Reinstall the plugin." >&2
    exit 1
fi
exec "$PLUGINBIN/venv/bin/python3" "$PLUGINBIN/miflora.py" "$@"
