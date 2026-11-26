#!/usr/bin/env bash
# Задача раннера: развернуть ассистент + Mailpit на pistonvlm (~/hackathon-app), проверить здоровье.
# Код — из клона решения на коммите задания; корпус — из INBOX_DIR/<папка>. gfreeze-r не трогает.
# Аргументы: <папка корпуса в INBOX_DIR>
set -euo pipefail
CORPUS="$INBOX_DIR/$1"
H=pistonvlm
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 $H"
echo "== порты 19650-19652 на стенде"
$SSH "ss -ltnp 2>/dev/null | grep -E ':(19650|19651|19652)\b' || echo свободны"
echo "== код и корпус"
$SSH "mkdir -p ~/hackathon-app/code ~/hackathon-app/corpus ~/hackathon-app/data/app ~/hackathon-app/data/mailpit"
git -C "$PWD" archive --format=tar HEAD | $SSH "rm -rf ~/hackathon-app/code.new && mkdir ~/hackathon-app/code.new && tar -x -C ~/hackathon-app/code.new && rm -rf ~/hackathon-app/code && mv ~/hackathon-app/code.new ~/hackathon-app/code"
tar -C "$CORPUS" -cf - . | $SSH "rm -rf ~/hackathon-app/corpus/* && tar -x -C ~/hackathon-app/corpus"
echo "== compose up"
$SSH "cd ~/hackathon-app/code/deploy && docker compose up -d --remove-orphans && docker compose ps"
echo "== ждём /api/health"
for i in $(seq 1 40); do
  if $SSH "curl -fsS --max-time 3 http://127.0.0.1:19650/api/health" 2>/dev/null; then echo; break; fi
  sleep 3
done
echo "== пробный вопрос"
$SSH "curl -fsS --max-time 60 -H 'Content-Type: application/json' -d '{\"question\":\"ДБРЦТ: кто создаёт заявку на пресейл?\"}' http://127.0.0.1:19650/api/ask" | python3 -c "
import json,sys; r=json.load(sys.stdin); print('outcome:', r['outcome'], 'seconds:', r['seconds']); print(json.dumps(r['answers'][:1], ensure_ascii=False)[:600])" | tee "$RUN_DIR/probe.txt"
$SSH "cd ~/hackathon-app/code/deploy && docker compose ps --format '{{.Name}} {{.Image}} {{.Status}}'" | tee "$RUN_DIR/ps.txt"
echo '{"deployed": true}' > "$RUN_DIR/metrics.json"
