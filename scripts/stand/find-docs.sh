#!/usr/bin/env bash
# Задача раннера (только чтение): список документов DOCX/PDF подборки doc-intel на стенде (DI0, G-TAB) — для теста
# «чужой регламент». Печатает путь, размер, страницы (PDF: pdfinfo; DOCX: число абзацев и знаков текста).
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
$SSH 'find ~ -maxdepth 9 -type f \( -iname "*.docx" -o -iname "*.pdf" \) 2>/dev/null | grep -iE "docintel|gtab|DI0|/O/" | grep -viE "raster|holdout|/gold/|node_modules|site-packages" | head -300 | while read -r f; do
  s=$(stat -c %s "$f"); p=""
  case "$f" in
    *.pdf|*.PDF) p=$(pdfinfo "$f" 2>/dev/null | awk "/^Pages:/{print \$2}");;
    *) p=$(unzip -p "$f" word/document.xml 2>/dev/null | python3 -c "import sys,re; x=sys.stdin.read(); t=re.sub(r\"<[^>]+>\",\"\",re.sub(r\"</w:p>\",\"\\n\",x)); print(\"абз\",t.count(\"\\n\"),\"зн\",len(t))");;
  esac
  echo "$s	$p	$f"; done' | tee "$RUN_DIR/docs.tsv"
echo '{}' > "$RUN_DIR/metrics.json"
