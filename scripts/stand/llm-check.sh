#!/usr/bin/env bash
# Задача раннера (только чтение): конфигурация vLLM на стенде — префиксный кэш, KV-кэш, загрузка.
set -euo pipefail
H=pistonvlm; SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 $H"
echo "== /metrics: конфигурация и кэш"
$SSH "curl -fsS --max-time 10 http://127.0.0.1:19643/metrics" | grep -E "^vllm:(cache_config_info|prefix_cache|kv_cache_usage|gpu_cache_usage|num_requests|num_gpu_blocks)|^# HELP vllm:prefix" | cut -c1-400 | tee "$RUN_DIR/metrics_vllm.txt"
echo "== аргументы запуска vLLM (контейнер, только чтение)"
$SSH "docker ps --format '{{.Names}} {{.Image}}' | grep -i vllm; for c in \$(docker ps --format '{{.Names}}' | grep -i vllm); do docker inspect --format '{{json .Args}}' \$c; done" | sed -E 's/(api[-_]key[\" ,:=]+)[^\" ,]+/\1***/Ig' | tee "$RUN_DIR/vllm_args.txt"
echo '{"checked": true}' > "$RUN_DIR/metrics.json"
