#!/usr/bin/env bash
# Задача раннера (только чтение): где на стенде лежат документы подборки doc-intel (DI0, G-TAB) и сами DOCX/PDF —
# для теста «чужой регламент». Печатает каталоги и файлы: размер, страницы PDF (pdfinfo), знаки текста DOCX.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
$SSH 'echo "== каталоги"; find / -xdev -maxdepth 8 -type d \( -name "docintel-2026-11" -o -name GTAB -o -name DI0 \) 2>/dev/null | head -20
echo "== файлы"; for d in $(find / -xdev -maxdepth 8 -type d \( -name "docintel-2026-11" -o -name GTAB -o -name DI0 \) 2>/dev/null | head -10); do
  find "$d" -maxdepth 6 -type f \( -iname "*.docx" -o -iname "*.pdf" \) 2>/dev/null | grep -viE "raster|holdout|/gold/" ; done | sort -u | head -400 | while read -r f; do
  s=$(stat -c %s "$f"); p=""
  case "$f" in
    *.pdf|*.PDF) p="стр $(pdfinfo "$f" 2>/dev/null | awk "/^Pages:/{print \$2}")";;
    *) p=$(unzip -p "$f" word/document.xml 2>/dev/null | python3 -c "import sys,re; x=sys.stdin.read(); t=re.sub(r\"<[^>]+>\",\"\",re.sub(r\"</w:p>\",\"\\n\",x)); print(\"зн\",len(t))");;
  esac
  echo "$s	$p	$f"; done
echo "== docx/pdf в домашнем каталоге (сколько)"; find ~ -xdev -maxdepth 7 -type f \( -iname "*.docx" -o -iname "*.pdf" \) 2>/dev/null | sed "s|/[^/]*$||" | sort | uniq -c | sort -rn | head -30' 2>&1 | tee "$RUN_DIR/docs.tsv"
echo '{}' > "$RUN_DIR/metrics.json"
