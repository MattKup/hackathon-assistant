#!/usr/bin/env bash
# Задача раннера: проверить загрузку и распознавание PDF через doc-intel в развёрнутом ассистенте, затем убрать документ.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
$SSH 'F=$(ls ~/hackathon-app/corpus/*.pdf | head -1); curl -sS -F "file=@$F;filename=test-upload.pdf" -F department=TECH -F stage=full_cycle http://127.0.0.1:19650/api/docs/upload; echo
for i in $(seq 1 60); do S=$(curl -sS http://127.0.0.1:19650/api/documents | python3 -c "import json,sys; d=[x for x in json.load(sys.stdin) if x[\"display_title\"].startswith(\"test-upload\")]; print(d[0][\"status\"] if d else \"нет\", d[0].get(\"pages\") if d else \"\", d[0].get(\"ocr_pages\") if d else \"\", d[0].get(\"windows\") if d else \"\", d[0][\"doc_id\"] if d else \"\")"); echo "$S"; case "$S" in распозна*) sleep 5;; *) break;; esac; done
ID=$(echo "$S" | awk "{print \$NF}")
curl -sS -H "Content-Type: application/json" --data-binary "{\"doc_id\":\"$ID\"}" http://127.0.0.1:19650/api/docs/remove; echo'
echo '{}' > "$RUN_DIR/metrics.json"
