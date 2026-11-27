#!/usr/bin/env bash
# Задача раннера: только чтение — какая версия страницы отдаётся (метки CSS) и заголовки кэша.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
$SSH "curl -sS -m 20 -D - http://127.0.0.1:19650/ -o /tmp/ui.html | head -12; grep -c 'right:12px;top:50%' /tmp/ui.html; grep -c 'min-width:212px' /tmp/ui.html; ss -ltnp 2>/dev/null | grep 8091; docker ps --format '{{.Names}} {{.Ports}}' | grep -i 8091" > "$RUN_DIR/ui.txt" 2>&1
echo '{}' > "$RUN_DIR/metrics.json"
