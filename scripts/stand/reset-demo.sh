#!/usr/bin/env bash
# Задача раннера: сброс демо-данных на стенде (то же, что кнопка «Сбросить демо-данные») и проверка, что пусто.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
OUT="$RUN_DIR/reset.txt"; : > "$OUT"
$SSH "curl -sS -m 120 -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:19650/api/admin/reset-demo; echo; curl -sS -m 20 http://127.0.0.1:19650/api/cases | head -c 300; echo; cd ~/hackathon-app/code/deploy 2>/dev/null && docker compose logs --tail 20 assistant 2>&1 | tail -20" | tee -a "$OUT"
echo '{}' > "$RUN_DIR/metrics.json"
