#!/usr/bin/env bash
# Задача раннера: энергия на вопрос. Пока идёт прогон эталона в 1 поток (как один запрос через веб),
# раз в секунду снимаем мощность видеокарты стенда (nvidia-smi power.draw); до и после — 20 с простоя.
# Аргументы: <папка корпуса> <eval.jsonl> — пути внутри INBOX_DIR.
set -uo pipefail
source "$(dirname "$0")/_python.sh"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 pistonvlm"
$SSH 'nohup sh -c "while true; do echo \$(date +%s.%N) \$(nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits | head -1); sleep 1; done" > /tmp/power-eval.log 2>&1 & echo $! > /tmp/power-eval.pid'
sleep 20
date +%s.%N > "$RUN_DIR/eval_start.txt"
"$PY" -I "$(dirname "$0")/../../scripts/eval/answer_eval.py" "$1" "$2" --workers=1
date +%s.%N > "$RUN_DIR/eval_end.txt"
sleep 20
$SSH 'kill $(cat /tmp/power-eval.pid) 2>/dev/null; cat /tmp/power-eval.log; rm -f /tmp/power-eval.log /tmp/power-eval.pid' > "$RUN_DIR/power.txt"
"$PY" -I - "$RUN_DIR" <<'EOF'
import json, sys
from pathlib import Path
d = Path(sys.argv[1])
t0, t1 = float((d / "eval_start.txt").read_text()), float((d / "eval_end.txt").read_text())
pts = []
for line in (d / "power.txt").read_text().split("\n"):
    p = line.split()
    if len(p) == 2:
        try:
            pts.append((float(p[0]), float(p[1])))
        except ValueError:
            pass
idle = [w for t, w in pts if t < t0 or t > t1]
busy = [(t, w) for t, w in pts if t0 <= t <= t1]
n = sum(1 for _ in open(d / "answers.jsonl"))
j = sum(busy[i][1] * (busy[i + 1][0] - busy[i][0]) for i in range(len(busy) - 1))   # Дж = Вт × с
idle_w = sum(idle) / len(idle) if idle else 0
m = json.loads((d / "metrics.json").read_text()) if (d / "metrics.json").exists() else {}
m.update({"power_idle_w": round(idle_w, 1), "power_busy_avg_w": round(sum(w for _, w in busy) / max(len(busy), 1), 1),
          "power_busy_max_w": max((w for _, w in busy), default=0), "eval_seconds": round(t1 - t0, 1), "questions": n,
          "gpu_wh_per_question": round(j / 3600 / max(n, 1), 3),
          "gpu_wh_per_question_above_idle": round((j - idle_w * (t1 - t0)) / 3600 / max(n, 1), 3)})
(d / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=2))
print({k: m[k] for k in ("power_idle_w", "power_busy_avg_w", "power_busy_max_w", "eval_seconds", "questions",
                         "gpu_wh_per_question", "gpu_wh_per_question_above_idle")})
EOF
