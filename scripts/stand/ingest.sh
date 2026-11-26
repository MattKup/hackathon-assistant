#!/usr/bin/env bash
# Задача раннера: скачать корпус организаторов (публичная папка Drive) на Mac mini
# и принять каждый файл через doc-intel. Разборы остаются на Mac mini (data/),
# в RUN_DIR пишутся только счётчики без текста документов.
# Аргументы: <drive_file_id>:<sha256> ... (sha256 сверяется после скачивания).
set -euo pipefail
exec python3 -I "$(dirname "$0")/../../app/ingest/run.py" "$@"
