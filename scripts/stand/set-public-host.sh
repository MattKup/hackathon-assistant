#!/usr/bin/env bash
# Задача раннера: поднять/обновить внешний адрес стенда (Caddy с паролем). Аргумент: публичный адрес (IP или имя).
# Пароль: генерируется на стенде один раз, лежит в ~/hackathon-public/secret/password (chmod 600) — в лог, git и чат НЕ выводится.
# Владелец читает его сам: ssh pistonvlm cat ~/hackathon-public/secret/password
set -euo pipefail
HOST_ARG="${1:?публичный адрес}"
H=pistonvlm; SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 $H"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
echo "== файлы"
$SSH "mkdir -p ~/hackathon-public/secret ~/hackathon-public/data ~/hackathon-public/config && chmod 700 ~/hackathon-public/secret"
cat "$ROOT/deploy/public/compose.yaml" | $SSH "cat > ~/hackathon-public/compose.yaml"
cat "$ROOT/deploy/public/Caddyfile.tpl" | $SSH "cat > ~/hackathon-public/Caddyfile.tpl"
echo "PUBLIC_HOST=$HOST_ARG" | $SSH "cat > ~/hackathon-public/public.env"
echo "== пароль (создаётся один раз) и Caddyfile"
$SSH "bash -s" <<'REMOTE'
set -euo pipefail
cd ~/hackathon-public
if [ ! -s secret/password ]; then
  umask 077; openssl rand -base64 18 | tr -d '/+=\n' | head -c 20 > secret/password; echo "пароль создан"
else
  echo "пароль уже есть — не меняю"
fi
HASH=$(docker run --rm caddy:2.11.9-alpine caddy hash-password --plaintext "$(cat secret/password)")
HOST=$(sed -n 's/^PUBLIC_HOST=//p' public.env)
python3 - "$HASH" "$HOST" <<'PY'
import sys, pathlib
h, host = sys.argv[1], sys.argv[2]
t = pathlib.Path("Caddyfile.tpl").read_text().replace("__HASH__", h).replace("__HOST__", host)
pathlib.Path("Caddyfile").write_text(t)
PY
echo "Caddyfile для $HOST готов"
docker compose -p hackathon-public up -d --force-recreate
sleep 3
docker compose -p hackathon-public ps --format '{{.Name}} {{.Image}} {{.Status}}'
echo "== проверка изнутри (пароль не печатается)"
echo -n "HTTP без пароля: "; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:80/
echo -n "HTTP с паролем:  "; curl -s -o /dev/null -w '%{http_code}\n' -u "jury:$(cat secret/password)" http://127.0.0.1:80/api/health
echo -n "HTTPS с паролем: "; curl -sk -o /dev/null -w '%{http_code}\n' --resolve "$HOST:443:127.0.0.1" -u "jury:$(cat secret/password)" "https://$HOST:443/api/health"
ss -ltnp 2>/dev/null | grep -E ':(80|443)\b' | awk '{print $1, $4}'
REMOTE
echo '{"public_host": "'"$HOST_ARG"'", "user": "jury"}' > "$RUN_DIR/metrics.json"
