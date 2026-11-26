#!/usr/bin/env bash
# Задача раннера: сквозной сценарий трёх кейсов на развёрнутом ассистенте.
set -euo pipefail
exec python3 -I "$(dirname "$0")/../../scripts/eval/scenario.py"
