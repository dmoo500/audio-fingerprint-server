#!/usr/bin/env bash
# Use the installed development tools without silently changing the environment.
set -euo pipefail
exec uv run --no-sync "$@"
