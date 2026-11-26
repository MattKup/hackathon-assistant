#!/usr/bin/env bash
# Задача раннера: сквозной сценарий трёх кейсов на развёрнутом ассистенте (через API на стенде).
# Кейс 1 (нет опоры → письмо), кейс 3 (ответ эксперта → практика → повторный вопрос), кейс 2 (уточнение).
set -euo pipefail
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
api(){ printf '%s' "$2" | $SSH "curl -fsS --max-time 90 -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:19650$1"; }
get(){ $SSH "curl -fsS --max-time 30 http://127.0.0.1:19650$1"; }
out="$RUN_DIR/scenario.jsonl"; : > "$out"
step(){ echo "== $1"; echo "{\"step\":\"$1\",\"resp\":$2}" >> "$out"; echo "$2" | python3 -c "import json,sys; r=json.load(sys.stdin); print({k:r.get(k) for k in ('outcome','text','case','seconds')}); [print('  ',a['label'],a['verdict'],a['summary'][:200]) for a in r.get('answers',[])]" 2>/dev/null || echo "$2" | head -c 400; echo; }
Q='Техническая дирекция: можно ли начать работы по гарантийному письму заказчика до подписания договора?'
step "1. вопрос без прямого ответа" "$(api /api/ask "{\"session\":\"s1\",\"question\":\"$Q\",\"employee\":\"ivanov@example.test\"}")"
CID=$(get /api/cases | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['case_id'])")
echo "case: $CID"
step "2. ответ эксперта (промежуточный)" "$(api /api/qa/reply "{\"case_id\":\"$CID\",\"text\":\"Добрый день! Уточняю у коллег из Технической дирекции, вернусь с ответом.\"}")"
step "3. обработка почты" "$(api /api/qa/poll '{}')"
step "4. ответ эксперта (итоговый)" "$(api /api/qa/reply "{\"case_id\":\"$CID\",\"text\":\"Да. Подписанное гарантийное письмо заказчика — основание для старта работ до подписания договора во всех дирекциях, включая Техническую. Старт согласует директор дирекции.\"}")"
step "5. обработка почты" "$(api /api/qa/poll '{}')"
step "6. практика" "$(get /api/practices)"
step "7. повторный вопрос другими словами" "$(api /api/ask "{\"session\":\"s2\",\"question\":\"В Техдирекции клиент прислал гарантийное письмо, договор ещё не подписан. Можно стартовать?\",\"employee\":\"petrov@example.test\"}")"
R8="$(api /api/ask "{\"session\":\"s3\",\"question\":\"За сколько заранее запускать заявку на расчёт?\"}")"
step "8. кейс 2: уточнение" "$R8"
OPT=$(echo "$R8" | python3 -c "import json,sys; r=json.load(sys.stdin); print(next((o['id'] for o in r['options'] if 'ДБРЦТ' in o['label']), ''))")
step "9. выбор ДБРЦТ" "$(api /api/ask "{\"session\":\"s3\",\"question\":\"\",\"choice\":\"$OPT\"}")"
echo "== письма в Mailpit"
$SSH "curl -fsS http://127.0.0.1:19652/api/v1/messages?limit=10" | python3 -c "import json,sys; [print(m['Created'][:19], m['From']['Address'], '→', [t['Address'] for t in m['To']], [c['Address'] for c in (m.get('Cc') or [])], m['Subject'][:70]) for m in json.load(sys.stdin)['messages']]" | tee "$RUN_DIR/mails.txt"
echo '{"ok": true}' > "$RUN_DIR/metrics.json"
