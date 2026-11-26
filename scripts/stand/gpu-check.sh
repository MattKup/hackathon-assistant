#!/usr/bin/env bash
# Задача раннера (только чтение): что занимает GPU стенда (5090 / 3090) — процессы, память, контейнеры и их модели.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
$SSH 'nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv; echo; nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory,process_name --format=csv; echo; nvidia-smi -L; echo "== контейнеры"; docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}"; echo "== GPU контейнеров и модели"; for c in $(docker ps --format "{{.Names}}"); do d=$(docker inspect -f "{{json .HostConfig.DeviceRequests}} {{.Config.Cmd}} {{range .Config.Env}}{{println .}}{{end}}" "$c" 2>/dev/null | grep -iE "device|cuda_visible|model|--model|served|gpu" | tr "\n" " " | cut -c1-400); echo "$c: $d"; done' 2>&1 | tee "$RUN_DIR/gpu.txt"
echo '{}' > "$RUN_DIR/metrics.json"
