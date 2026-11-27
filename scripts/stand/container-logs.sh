#!/usr/bin/env bash
# Задача раннера (только чтение): хвост журнала контейнера стенда и его состояние. Аргумент — имя контейнера (по умолчанию embed).
set -uo pipefail
C="${1:-gfreeze-r-embed-1}"
case "$C" in *[!A-Za-z0-9_.-]*) echo "bad name"; exit 2;; esac
ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm "docker inspect -f '{{.State.Status}} restarts={{.RestartCount}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} err={{.State.Error}} started={{.State.StartedAt}}' $C; echo; docker logs --tail 60 $C 2>&1; echo; nvidia-smi --query-gpu=memory.used,memory.total --format=csv" 2>&1 | tee "$RUN_DIR/container.txt"
echo '{}' > "$RUN_DIR/metrics.json"
