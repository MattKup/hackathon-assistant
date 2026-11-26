#!/usr/bin/env bash
# Задача: нагрузочный замер — запросов в час при 1/2/4/8 одновременных пользователях (для экономики).
set -euo pipefail
source "$(dirname "$0")/_python.sh"
exec "$PY" -I "$(dirname "$0")/../../scripts/eval/load_test.py" "$@"
