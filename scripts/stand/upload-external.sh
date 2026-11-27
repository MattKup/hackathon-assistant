#!/usr/bin/env bash
# Задача раннера: загрузить внешние источники (docs/examples/*.txt) в развёрнутый ассистент с типом «Внешний источник»
# — для блока «Обратите внимание — не из регламента». Повторная загрузка того же файла не дублирует (doc_id = sha256).
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
OUT="$RUN_DIR/external.txt"; : > "$OUT"
for f in docs/examples/*.txt; do
  name=$(basename "$f")
  $SSH "cat > /tmp/$name" < "$f"
  R=$($SSH "curl -sS -F 'file=@/tmp/$name;filename=$name' -F doc_kind=external -F stage=full_cycle http://127.0.0.1:19650/api/docs/upload; rm -f /tmp/$name")
  echo "$name: $R" | tee -a "$OUT"
done
sleep 5
$SSH "curl -sS http://127.0.0.1:19650/api/documents" | python3 -c "import json,sys; d=[x for x in json.load(sys.stdin) if x.get('department')=='EXTERNAL']; print(json.dumps([[x['display_title'], x['status'], x.get('windows')] for x in d], ensure_ascii=False))" | tee -a "$OUT"
echo '{}' > "$RUN_DIR/metrics.json"
