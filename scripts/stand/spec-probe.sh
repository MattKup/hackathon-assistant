#!/usr/bin/env bash
# Задача раннера (только чтение): что нужно для спекулятивного декодирования в vLLM на стенде.
# Аргументы запуска контейнера LLM, compose-файл (значения секретов маскируются), версия vLLM,
# есть ли в модели MTP-слои (config.json и индекс весов). Ничего не перезапускает.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
OUT="$RUN_DIR/spec_probe.txt"; : > "$OUT"
mask() { sed -E 's/((api[-_]?key|token|secret|password|passwd|HF_TOKEN)[^=:]*[=:][[:space:]]*)[^[:space:]",]+/\1***/Ig'; }
C=gfreeze-r-llm-1
{
echo "== контейнер $C"
$SSH "docker inspect --format 'image={{.Config.Image}}
cmd={{json .Config.Cmd}}
entrypoint={{json .Config.Entrypoint}}
args={{json .Args}}
compose_files={{index .Config.Labels \"com.docker.compose.project.config_files\"}}
compose_dir={{index .Config.Labels \"com.docker.compose.project.working_dir\"}}
service={{index .Config.Labels \"com.docker.compose.service\"}}
mounts={{json .Mounts}}' $C"
echo "== версия vLLM"
$SSH "docker exec $C python3 -c 'import vllm; print(vllm.__version__)'" 2>&1
echo "== compose-файл(ы)"
F=$($SSH "docker inspect --format '{{index .Config.Labels \"com.docker.compose.project.config_files\"}}' $C")
for f in ${F//,/ }; do echo "--- $f"; $SSH "cat '$f'"; done
echo "== модель: путь из /v1/models"
$SSH "curl -sS --max-time 10 http://127.0.0.1:19643/v1/models"
echo
echo "== MTP в config.json и в весах (внутри контейнера)"
$SSH "docker exec $C sh -c 'for d in \$(find / -maxdepth 4 -name config.json -path \"*models*\" 2>/dev/null | xargs -n1 dirname | sort -u); do echo \"--- \$d\"; grep -oE \"\\\"(architectures|model_type|mtp[a-z_]*|num_nextn_predict_layers|quant_method|bits)\\\"[^,}]*\" \$d/config.json | head -20; ls \$d | head -20; if [ -f \$d/model.safetensors.index.json ]; then echo mtp_keys=\$(grep -c mtp \$d/model.safetensors.index.json); fi; done'" 2>&1
echo "== память GPU"
$SSH "nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv"
} 2>&1 | mask | tee "$OUT"
echo '{"checked": true}' > "$RUN_DIR/metrics.json"
