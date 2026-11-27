#!/usr/bin/env bash
# Задача раннера: качество на базе с настоящими «чужими» документами (не копиями).
# Корпус = наши регламенты + N реальных документов со стенда (каждая папка-источник — своё подразделение) → прогон эталона.
# Аргументы: <папка корпуса> <eval.jsonl> [N=40] — пути внутри INBOX_DIR. Ответы эталона — только из наших регламентов,
# чужие документы — помехи для поиска. Корпус собирается во временной папке вне клона раннера (клон не пачкаем).
set -euo pipefail
source "$(dirname "$0")/_python.sh"
CORPUS="$1"; EVAL="$2"; N="${3:-40}"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/c"; cp "$INBOX_DIR/$CORPUS"/*.docx "$TMP/c/"; ln -s "$INBOX_DIR/eval" "$TMP/eval"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
pick() {  # $1 каталог на стенде, $2 код подразделения, $3 название, $4 сколько
  $SSH "find ~/$1 -maxdepth 3 -type f \( -iname '*.docx' -o -iname '*.pdf' \) ! -name '._*' -size +20k -size -15M 2>/dev/null | sort | head -$4" |
  while read -r f; do
    b="$2-$(basename "$f")"
    scp -q -o BatchMode=yes "pistonvlm:$f" "$TMP/c/$b" || continue
    printf '{"department": "%s", "department_name": "%s", "stage": "full_cycle", "title": "%s"}\n' "$2" "$3" "${b%.*}" > "$TMP/c/$b.meta.json"
  done
}
pick tenderlov-demo-data TEND "Тендерная документация" $((N / 4))
pick tenderlov-demo/fixtures TENDF "Тендеры (архив)" $((N / 4))
pick gret-prep/data/sources VEND "Документация вендоров" $((N - 2 * (N / 4)))
echo "корпус: $(ls "$TMP/c" | grep -vc meta.json) документов (наших: $(ls "$INBOX_DIR/$CORPUS"/*.docx | wc -l))" | tee "$RUN_DIR/corpus.txt"
ls "$TMP/c" | grep -v meta.json >> "$RUN_DIR/corpus.txt"
CORPUS_PDF=1 INBOX_DIR="$TMP" "$PY" -I "$(dirname "$0")/../../scripts/eval/answer_eval.py" c "$EVAL"
