#!/usr/bin/env bash
# Задача раннера: только чтение — карточки знаний, обращения, хвост лога ассистента.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
$SSH "curl -sS -m 20 http://127.0.0.1:19650/api/practices; echo; echo ==cases; curl -sS -m 20 http://127.0.0.1:19650/api/cases | head -c 3000; echo; cd ~/hackathon-app/code/deploy 2>/dev/null && docker compose logs --tail 200 assistant 2>&1" > "$RUN_DIR/state.txt" 2>&1
echo '{}' > "$RUN_DIR/metrics.json"
