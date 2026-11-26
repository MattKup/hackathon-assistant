#!/usr/bin/env bash
# Задача раннера: логи и состояние ассистента на стенде (только чтение).
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
$SSH "cd ~/hackathon-app/code/deploy && docker compose ps && docker compose logs --tail 80 assistant; curl -sS -m 30 -H 'Content-Type: application/json' -d '{\"question\":\"тест: можно ли начать работы по гарантийному письму?\"}' http://127.0.0.1:19650/api/ask | head -c 600; echo; docker compose logs --tail 30 assistant"
echo '{}' > "$RUN_DIR/metrics.json"
