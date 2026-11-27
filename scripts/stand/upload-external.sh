#!/usr/bin/env bash
# Задача раннера: загрузить внешние источники (docs/examples/*.txt) в развёрнутый ассистент с типом «Внешний источник»
# для блока «Обратите внимание, не из регламента». Имя в реестре берётся из docs/examples/titles.tsv
# (файл → человеческое название); прежняя запись того же файла (doc_id = sha256 содержимого) сначала убирается,
# чтобы название обновилось.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
OUT="$RUN_DIR/external.txt"; : > "$OUT"
for f in docs/examples/*.txt; do
  name=$(basename "$f")
  title=$(awk -F'\t' -v n="$name" '$1==n {print $2}' docs/examples/titles.tsv 2>/dev/null)
  [ -n "$title" ] || title="${name%.txt}"
  sha=$(shasum -a 256 "$f" | cut -d' ' -f1)
  $SSH "curl -sS -H 'Content-Type: application/json' -d '{\"doc_id\":\"$sha\"}' http://127.0.0.1:19650/api/docs/remove >/dev/null"
  $SSH "cat > /tmp/ext_upload.txt" < "$f"
  R=$($SSH "curl -sS -F \"file=@/tmp/ext_upload.txt;filename=\\\"$title.txt\\\"\" -F doc_kind=external -F stage=full_cycle http://127.0.0.1:19650/api/docs/upload; rm -f /tmp/ext_upload.txt")
  echo "$name → «$title»: $R" | tee -a "$OUT"
done
sleep 5
$SSH "curl -sS http://127.0.0.1:19650/api/documents" | python3 -c "import json,sys; d=[x for x in json.load(sys.stdin) if x.get('department')=='EXTERNAL']; print(json.dumps([[x['display_title'], x['status'], x.get('windows')] for x in d], ensure_ascii=False))" | tee -a "$OUT"
echo '{}' > "$RUN_DIR/metrics.json"
