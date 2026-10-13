#!/usr/bin/env bash
# Scan Ubuntu runner labels without changing workflows.
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is required for the Ubuntu runner audit." >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/audit_ubuntu_runner.py" "$@"
