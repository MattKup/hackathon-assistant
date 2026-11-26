#!/usr/bin/env bash
# Задача раннера (только чтение): публичный IP стенда, занятость 80/443, наличие Caddy.
set -uo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
$SSH 'echo "public: $(curl -s -m 8 https://api.ipify.org)"; echo "--- 80/443:"; ss -ltnp 2>/dev/null | grep -E ":(80|443)\b" || echo свободны; echo "--- caddy:"; docker ps --format "{{.Names}} {{.Image}}" | grep -i caddy || echo нет; which caddy || true; echo "--- ufw:"; sudo -n ufw status 2>/dev/null | head -5 || echo "нет доступа к ufw"; ip -4 addr show scope global | grep inet'
echo '{}' > "$RUN_DIR/metrics.json"
