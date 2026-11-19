#!/usr/bin/env sh
# SPDX-License-Identifier: Apache-2.0
# Shim: make sure uv is available, then hand over to rezbox.py.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    if [ -x "$HOME/.local/bin/uv" ]; then
        PATH="$HOME/.local/bin:$PATH"
    else
        echo "[rezbox] uv not found, installing it (https://docs.astral.sh/uv/)" >&2
        curl -LsSf https://astral.sh/uv/install.sh | sh
        PATH="$HOME/.local/bin:$PATH"
    fi
fi

exec uv run --quiet --script "$HERE/rezbox.py" "$@"
