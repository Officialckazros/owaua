#!/usr/bin/env bash
set -euo pipefail

# Explicit Daki entry point. The implementation lives in deploy.sh for
# backwards compatibility with existing operator instructions.
exec "$(cd "$(dirname "$0")" && pwd -P)/deploy.sh" "$@"
