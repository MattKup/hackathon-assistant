#!/usr/bin/env bash
# Задача раннера (только чтение на стенде): скопировать документ со стенда в RUN_DIR (для теста «чужой регламент»).
# Аргумент: путь относительно домашнего каталога на стенде.
set -euo pipefail
scp -o BatchMode=yes -o ConnectTimeout=15 "pistonvlm:$1" "$RUN_DIR/"
ls -la "$RUN_DIR"
echo '{}' > "$RUN_DIR/metrics.json"
