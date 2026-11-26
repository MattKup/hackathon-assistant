#!/usr/bin/env bash
# Задача раннера (только чтение): список DOCX/PDF в каталогах стенда — для теста «чужой регламент».
# Аргументы: каталоги относительно домашнего (по умолчанию — поиск каталогов с документами по всему ~).
# Печатает размер, страницы PDF (pdfinfo), знаки текста DOCX, путь.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
DIRS="${*:-}"
$SSH "DIRS='$DIRS'"'
if [ -z "$DIRS" ]; then find ~ -xdev -maxdepth 7 -type f \( -iname "*.docx" -o -iname "*.pdf" \) 2>/dev/null | sed "s|/[^/]*$||" | sort | uniq -c | sort -rn | head -30; exit 0; fi
for d in $DIRS; do find ~/"$d" -maxdepth 3 -type f \( -iname "*.docx" -o -iname "*.pdf" \) 2>/dev/null; done | sort -u | head -300 | while read -r f; do
  s=$(stat -c %s "$f"); p=""
  case "$f" in
    *.pdf|*.PDF) p="стр $(pdfinfo "$f" 2>/dev/null | awk "/^Pages:/{print \$2}")";;
    *) p=$(unzip -p "$f" word/document.xml 2>/dev/null | python3 -c "import sys,re; x=sys.stdin.read(); t=re.sub(r\"<[^>]+>\",\"\",re.sub(r\"</w:p>\",\"\\n\",x)); n=len(re.findall(r\"(?m)^\\s*\\d+(\\.\\d+)+\\.?\\s\",t)); print(\"зн\",len(t),\"пунктов\",n)");;
  esac
  echo "$s	$p	$f"; done' 2>&1 | tee "$RUN_DIR/docs.tsv"
echo '{}' > "$RUN_DIR/metrics.json"
