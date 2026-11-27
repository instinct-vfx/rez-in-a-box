#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Run rezbox.py from the read-only repo mount.
set -eu
if [ ! -f /rezbox/rezbox.py ]; then
    echo "[rezbox] /rezbox is not mounted (run through: rezbox <command> --target linux)" >&2
    exit 2
fi
mkdir -p "$REZBOX_HOME"
exec uv run --quiet --no-project --script /rezbox/rezbox.py "$@"
