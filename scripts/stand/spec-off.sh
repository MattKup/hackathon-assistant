#!/usr/bin/env bash
# Задача раннера: откат спекулятивного декодирования — удалить копию gfreeze-spec-llm, запустить исходный gfreeze-r-llm-1.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
OUT="$RUN_DIR/spec_off.txt"; : > "$OUT"
$SSH "curl -fsS --max-time 10 http://127.0.0.1:19643/metrics" 2>/dev/null | grep -E "^vllm:spec_decode" | cut -c1-200 | tee -a "$OUT"
$SSH "docker rm -f gfreeze-spec-llm 2>&1; docker start gfreeze-r-llm-1" | tee -a "$OUT"
for i in $(seq 1 90); do
  if $SSH "curl -fsS --max-time 5 http://127.0.0.1:19643/v1/models >/dev/null"; then
    echo "исходный vLLM отвечает через $((i*10)) с" | tee -a "$OUT"; echo '{"restored": true}' > "$RUN_DIR/metrics.json"; exit 0
  fi
  sleep 10
done
echo "исходный vLLM не ответил за 15 мин" | tee -a "$OUT"; echo '{"restored": false}' > "$RUN_DIR/metrics.json"; exit 1
