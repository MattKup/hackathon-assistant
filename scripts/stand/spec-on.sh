#!/usr/bin/env bash
# Задача раннера: включить спекулятивное декодирование vLLM на стенде (по умолчанию ngram, без доп. памяти).
# Исходный контейнер gfreeze-r-llm-1 только ОСТАНАВЛИВАЕТСЯ (не удаляется). Рядом поднимается копия
# gfreeze-spec-llm с теми же образом, аргументами, окружением, томами, портами и GPU плюс --speculative-config.
# Если копия не поднялась или не отвечает по JSON-схеме — сразу откат (копия удаляется, исходный стартует).
# Откат вручную: scripts/stand/spec-off.sh. Аргумент 1 (необязательно) — JSON speculative-config.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
ORIG=gfreeze-r-llm-1; SPEC=gfreeze-spec-llm
CFG=${1:-'{"method":"ngram","num_speculative_tokens":4,"prompt_lookup_min":2,"prompt_lookup_max":5}'}
OUT="$RUN_DIR/spec_on.txt"; : > "$OUT"
log() { echo "$*" | tee -a "$OUT"; }

revert() {
  log "== откат: $1"
  $SSH "docker rm -f $SPEC >/dev/null 2>&1; docker start $ORIG" | tee -a "$OUT"
  for i in $(seq 1 90); do
    $SSH "curl -fsS --max-time 5 http://127.0.0.1:19643/v1/models >/dev/null" && { log "исходный vLLM снова отвечает через $((i*10)) с"; break; }
    sleep 10
  done
  echo "{\"spec\": false, \"reason\": \"$1\"}" > "$RUN_DIR/metrics.json"; exit 1
}

log "== speculative-config: $CFG"
$SSH "docker inspect $ORIG" > "$RUN_DIR/.inspect.json" || { log "нет контейнера $ORIG"; exit 1; }
# Собираем docker run (в журнал не пишется: в окружении могут быть секреты)
python3 - "$RUN_DIR/.inspect.json" "$CFG" "$SPEC" > "$RUN_DIR/.run.sh" <<'PY'
import json, shlex, sys
c = json.load(open(sys.argv[1]))[0]; cfg = sys.argv[2]; name = sys.argv[3]
C, H = c["Config"], c["HostConfig"]
a = ["docker", "run", "-d", "--name", name]
for e in C.get("Env") or []: a += ["-e", e]
for m in c.get("Mounts") or []:
    if m["Type"] == "bind": a += ["-v", f'{m["Source"]}:{m["Destination"]}' + ("" if m.get("RW", True) else ":ro")]
    elif m["Type"] == "volume": a += ["-v", f'{m["Name"]}:{m["Destination"]}' + ("" if m.get("RW", True) else ":ro")]
for port, binds in (H.get("PortBindings") or {}).items():
    for b in binds or []:
        ip = b.get("HostIp") or ""; a += ["-p", (f"{ip}:" if ip else "") + f'{b["HostPort"]}:{port}']
for d in H.get("DeviceRequests") or []:
    ids = d.get("DeviceIDs") or []
    a += ["--gpus", ("device=" + ",".join(ids)) if ids else "all"]
if H.get("IpcMode") in ("host", "shareable"): a += ["--ipc", "host"] if H["IpcMode"] == "host" else []
if H.get("ShmSize"): a += ["--shm-size", str(H["ShmSize"])]
net = H.get("NetworkMode") or "default"
if net not in ("default", "bridge"): a += ["--network", net]
svc = (C.get("Labels") or {}).get("com.docker.compose.service")
if svc and net not in ("host", "default", "bridge"): a += ["--network-alias", svc]
for u in H.get("Ulimits") or []: a += ["--ulimit", f'{u["Name"]}={u["Soft"]}:{u["Hard"]}']
ep = C.get("Entrypoint") or []
if ep: a += ["--entrypoint", ep[0]]
a += [C["Image"]] + ep[1:] + (C.get("Cmd") or []) + ["--speculative-config", cfg]
print(" ".join(shlex.quote(x) for x in a))
PY
[ -s "$RUN_DIR/.run.sh" ] || { log "не удалось собрать команду"; exit 1; }
log "== аргументы копии (без окружения):"
python3 -c "import json,sys;c=json.load(open(sys.argv[1]))[0]['Config'];print(c['Image'], c.get('Entrypoint'), c.get('Cmd'))" "$RUN_DIR/.inspect.json" | tee -a "$OUT"

log "== останавливаю $ORIG (не удаляю), запускаю $SPEC"
$SSH "docker stop $ORIG" | tee -a "$OUT"
$SSH "docker rm -f $SPEC >/dev/null 2>&1; bash -s" < "$RUN_DIR/.run.sh" | tee -a "$OUT" || revert "docker run не прошёл"
rm -f "$RUN_DIR/.run.sh" "$RUN_DIR/.inspect.json"

ok=0
for i in $(seq 1 90); do
  $SSH "curl -fsS --max-time 5 http://127.0.0.1:19643/v1/models >/dev/null" && { ok=1; log "копия отвечает через $((i*10)) с"; break; }
  $SSH "docker ps -q -f name=$SPEC -f status=running" | grep -q . || break
  sleep 10
done
$SSH "docker logs --tail 60 $SPEC 2>&1" | grep -iE "specul|ngram|error|exception|not support" | tail -15 | tee -a "$OUT"
[ $ok = 1 ] || revert "копия не поднялась за 15 мин"

MODEL=$($SSH "curl -fsS http://127.0.0.1:19643/v1/models" | python3 -c "import json,sys;print(json.load(sys.stdin)['data'][0]['id'])")
log "== проверка ответа по JSON-схеме и скорости (модель $MODEL)"
BODY=$(python3 - "$MODEL" <<'PY'
import json, sys
print(json.dumps({"model": sys.argv[1], "temperature": 0, "max_tokens": 300,
  "messages": [{"role": "user", "content": "Перепиши дословно и верни в поле quote: «Руководитель проекта согласует смету проекта с Дирекцией информационной безопасности до выхода на площадку заказчика.»"}],
  "response_format": {"type": "json_schema", "json_schema": {"name": "t", "schema": {"type": "object", "properties": {"quote": {"type": "string"}}, "required": ["quote"]}}}}, ensure_ascii=False))
PY
)
for k in 1 2 3; do
  R=$($SSH "curl -sS --max-time 120 -w '\n%{time_total}' -H 'Content-Type: application/json' -d @- http://127.0.0.1:19643/v1/chat/completions" <<< "$BODY")
  T=$(tail -1 <<< "$R"); J=$(sed '$d' <<< "$R")
  echo "$J" | python3 -c "import json,sys;d=json.load(sys.stdin);m=d['choices'][0]['message']['content'];json.loads(m);print('ok', len(m), 'знаков,', d['usage']['completion_tokens'], 'токенов')" 2>&1 | sed "s/$/, $T с/" | tee -a "$OUT"
  echo "$J" | python3 -c "import json,sys;json.loads(json.load(sys.stdin)['choices'][0]['message']['content'])" 2>/dev/null || revert "ответ по JSON-схеме не прошёл"
done
$SSH "curl -fsS --max-time 10 http://127.0.0.1:19643/metrics" | grep -E "^vllm:spec_decode" | cut -c1-200 | tee -a "$OUT"
echo "{\"spec\": true}" > "$RUN_DIR/metrics.json"
log "== включено. Откат: задача spec-off"
