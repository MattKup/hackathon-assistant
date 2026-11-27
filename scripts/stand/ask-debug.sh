#!/usr/bin/env bash
# Задача раннера: задать один вопрос развёрнутому помощнику и сохранить полный ответ (шаги, отброшенные шаги с причиной,
# трасса) — для разбора, почему ответ не прошёл проверки. Аргумент 1 — вопрос. Создаёт обычный ход (и обращение, если
# вопрос уйдёт в Службу качества): перед показом — «Сбросить демо-данные».
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
Q="${1:-}"; [ -z "$Q" ] || [ "$Q" = file ] && Q=$(head -1 "$(dirname "$0")/ask_debug_question.txt")
BODY=$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1]}, ensure_ascii=False))' "$Q")
$SSH "curl -sS -m 120 -H 'Content-Type: application/json' -d @- http://127.0.0.1:19650/api/ask" <<< "$BODY" > "$RUN_DIR/answer.json"
python3 - "$RUN_DIR/answer.json" <<'PY' | tee "$RUN_DIR/summary.txt"
import json, sys
d = json.load(open(sys.argv[1]))
print("исход:", d.get("outcome"), "|", (d.get("text") or "")[:200])
for a in d.get("answers") or []:
    print("==", a.get("label"), a.get("verdict"), "|", (a.get("summary") or "")[:200])
    for s in a.get("steps") or []:
        print("  шаг:", s.get("clause"), "|", (s.get("text") or "")[:160], "| цитата:", (s.get("quote") or "")[:160])
    for s in a.get("rejected") or []:
        print("  ОТБРОШЕН:", s.get("reason"), "заменён цитатой" if s.get("replaced") else "", "|", s.get("text", "")[:160], "| цитата:", s.get("quote", "")[:200])
for t in d.get("trace") or []:
    print("  трасса:", t[:200])
PY
echo '{}' > "$RUN_DIR/metrics.json"
