#!/usr/bin/env bash
# Задача раннера: чистый перезапуск стенда перед показом (после многих прогонов возможен OOM).
# Порядок (STAND_LOG / README «Запуск»): копия vLLM со спекулятивным декодированием убирается, если осталась;
# модель останавливается → эмбеддинги и реранкер перезапускаются первыми (их прогреву нужна свободная память) →
# модель → помощник и Mailpit. Затем проверка здоровья всех сервисов и память GPU. Тома и образы не трогаются.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
OUT="$RUN_DIR/restart.txt"; : > "$OUT"
log() { echo "$*" | tee -a "$OUT"; }
wait_url() {   # $1 — имя, $2 — адрес, $3 — сколько ждать (по 10 с)
  for i in $(seq 1 "$3"); do
    if $SSH "curl -fsS --max-time 5 '$2' >/dev/null"; then log "$1: отвечает через $((i*10)) с"; return 0; fi
    sleep 10
  done
  log "$1: НЕ отвечает за $(( $3 * 10 )) с"; return 1
}
log "== до: память GPU и контейнеры"
$SSH "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader; docker ps --format '{{.Names}} {{.Status}}'" | tee -a "$OUT"
log "== копия vLLM со спекулятивным декодированием (если есть) — удаляю"
$SSH "docker rm -f gfreeze-spec-llm 2>/dev/null || true" | tee -a "$OUT"
TEI=$($SSH "docker ps -a --format '{{.Names}}' | grep -E '^gfreeze-r-' | grep -iE 'tei|embed|rerank'" | tr '\n' ' ')
log "== останавливаю модель (gfreeze-r-llm-1), перезапускаю поиск: $TEI"
$SSH "docker stop gfreeze-r-llm-1" | tee -a "$OUT"
[ -n "$TEI" ] && $SSH "docker restart $TEI" | tee -a "$OUT"
ok=1
wait_url "эмбеддинги" http://127.0.0.1:19641/health 30 || ok=0
wait_url "реранкер" http://127.0.0.1:19642/health 30 || ok=0
log "== запускаю модель"
$SSH "docker start gfreeze-r-llm-1" | tee -a "$OUT"
wait_url "модель" http://127.0.0.1:19643/v1/models 60 || ok=0
log "== перезапускаю помощника и Mailpit"
$SSH "docker restart hackathon-app-assistant-1 hackathon-app-mailpit-1" | tee -a "$OUT"
wait_url "помощник" http://127.0.0.1:19650/api/health 30 || ok=0
wait_url "doc-intel" http://127.0.0.1:19640/health 6 || log "doc-intel: проверить вручную (адрес здоровья может отличаться)"
log "== после: память GPU и контейнеры"
$SSH "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader; docker ps --format '{{.Names}} {{.Status}}'" | tee -a "$OUT"
echo "{\"restarted\": true, \"healthy\": $([ $ok = 1 ] && echo true || echo false)}" > "$RUN_DIR/metrics.json"
[ $ok = 1 ]
