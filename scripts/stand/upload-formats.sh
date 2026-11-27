#!/usr/bin/env bash
# Задача раннера: загрузить синтетические файлы разных форматов (tests/fixtures/formats) в развёрнутый ассистент,
# дождаться разбора (DOC/RTF/ODT — через doc-intel, TXT/MD — сразу), записать статус и число пунктов, затем убрать.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
OUT="$RUN_DIR/formats.txt"; : > "$OUT"
for f in tests/fixtures/formats/formats-test.*; do
  ext="${f##*.}"
  $SSH "cat > /tmp/formats-test.$ext" < "$f"
  R=$($SSH "curl -sS -F 'file=@/tmp/formats-test.$ext;filename=formats-test.$ext' -F department_name='Тест форматов' -F stage=full_cycle http://127.0.0.1:19650/api/docs/upload")
  ID=$(echo "$R" | python3 -c "import json,sys; print(json.load(sys.stdin).get('doc_id',''))" 2>/dev/null)
  S="нет ответа: $R"
  for i in $(seq 1 60); do
    S=$($SSH "curl -sS http://127.0.0.1:19650/api/documents" | python3 -c "import json,sys; d=[x for x in json.load(sys.stdin) if x['doc_id']=='$ID']; print((d[0]['status']+' пунктов='+str(d[0].get('windows'))+' стр='+str(d[0].get('pages'))) if d else 'нет')")
    case "$S" in распозна*) sleep 5;; *) break;; esac
  done
  echo "$ext: $S" | tee -a "$OUT"
  [ -n "$ID" ] && $SSH "curl -sS -H 'Content-Type: application/json' --data-binary '{\"doc_id\":\"$ID\"}' http://127.0.0.1:19650/api/docs/remove >/dev/null; rm -f /tmp/formats-test.$ext"
done
python3 - "$OUT" > "$RUN_DIR/metrics.json" <<'EOF'
import json, sys
rows = dict(l.split(": ", 1) for l in open(sys.argv[1]).read().splitlines() if ": " in l)
print(json.dumps({"formats": rows, "ok": sorted(k for k, v in rows.items() if v.startswith("действует"))}, ensure_ascii=False))
EOF
