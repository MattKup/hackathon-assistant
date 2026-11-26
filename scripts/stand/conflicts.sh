#!/usr/bin/env bash
# Задача раннера: поиск противоречий между пунктами регламентов для службы качества.
# По умолчанию — ночной режим «точность выше стоимости»: все пары после фильтра, мягкий судья + три перепроверки.
#   bash scripts/stand/conflicts.sh <корпус в INBOX_DIR>                    — ночной режим
#   bash scripts/stand/conflicts.sh <корпус> 300                            — быстрый, бюджет 300 пар, без перепроверки
#   bash scripts/stand/conflicts.sh <корпус> prev=<metrics.json прошлого>  — только пары новых/изменённых документов
#   доп.: changed=<префикс doc_id>,…  workers=6  --no-dense  --offline
# Запуск по расписанию (ночью / раз в неделю): см. README, раздел «Анализатор противоречий». Файлы — в RUN_DIR.
set -euo pipefail
exec python3 -I "$(dirname "$0")/../../scripts/eval/conflicts.py" "$@"
