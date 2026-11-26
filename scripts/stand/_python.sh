# Выбор интерпретатора для задач раннера: первый, где есть numpy (ускоритель поиска), иначе python3.
# Порядок: $HACKATHON_PYTHON → miniconda/anaconda (base) → python3. Homebrew-python не трогаем, раннер не перезапускаем.
pick_python() {
  for p in "${HACKATHON_PYTHON:-}" "$HOME/miniconda3/bin/python3" "$HOME/opt/miniconda3/bin/python3" \
           "$HOME/anaconda3/bin/python3" "/opt/miniconda3/bin/python3" "/opt/homebrew/Caskroom/miniconda/base/bin/python3" python3; do
    [ -n "$p" ] || continue
    command -v "$p" >/dev/null 2>&1 || [ -x "$p" ] || continue
    if "$p" -I -c "import numpy" 2>/dev/null; then echo "$p"; return; fi
  done
  echo python3
}
PY="$(pick_python)"
echo "python: $PY ($("$PY" -I -c 'import sys;print(sys.version.split()[0])'); numpy: $("$PY" -I -c 'import numpy;print(numpy.__version__)' 2>/dev/null || echo нет))" >&2
