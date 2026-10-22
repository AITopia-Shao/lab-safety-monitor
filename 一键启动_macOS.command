#!/bin/bash
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || exit 1
exec /bin/bash "$PROJECT_DIR/labguard/macos/launch.sh"  "$@"
