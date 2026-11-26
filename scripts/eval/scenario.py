"""Сквозной сценарий трёх кейсов на развёрнутом ассистенте (стенд, через ssh + curl, тело — stdin)."""
import json
import os
import subprocess
import sys
from pathlib import Path

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "pistonvlm"]
RUN = Path(os.environ.get("RUN_DIR", "."))
LOG = open(RUN / "scenario.jsonl", "w")


def call(path, body=None):
    cmd = "curl -sS --max-time 120 http://127.0.0.1:19650" + path
    if body is not None:
        cmd = "curl -sS --max-time 120 -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:19650" + path
    r = subprocess.run(SSH + [cmd], input=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                       capture_output=True, timeout=180)
    return json.loads(r.stdout or b"{}")


def step(name, resp):
    LOG.write(json.dumps({"step": name, "resp": resp}, ensure_ascii=False) + "\n")
    print("==", name)
    if isinstance(resp, dict):
        print("  ", {k: resp.get(k) for k in ("outcome", "text", "case", "seconds", "events", "error") if k in resp})
        for a in resp.get("answers", []):
            print("    ", a["label"], a["verdict"], "|", a["summary"][:220])
            for s in a["steps"]:
                print("       •", s["text"][:150], f"(п. {s['clause']}{', практика' if s.get('practice') else ''})")
    else:
        print("  ", json.dumps(resp, ensure_ascii=False)[:500])
    return resp


step("0. сброс демо-данных", call("/api/admin/reset-demo", {}))
Q1 = "Техническая дирекция: можно ли начать работы по гарантийному письму заказчика до подписания договора?"
r = step("1. кейс 1: вопрос без прямого ответа в регламенте", call("/api/ask", {"session": "s1", "question": Q1, "employee": "ivanov@example.test"}))
if r.get("outcome") != "escalate":
    step("1a. сотрудник: «Уточнить у службы качества»", call("/api/escalate", {"session": "s1"}))
cases = call("/api/cases")
cid = cases[0]["case_id"] if cases else None
print("   обращение:", cid, cases[0]["status"] if cases else "")
if cid:
    step("2. эксперт: промежуточный ответ", call("/api/qa/reply", {"case_id": cid, "text": "Добрый день! Уточняю у коллег из Технической дирекции, вернусь с ответом."}))
    step("3. бот разбирает почту", call("/api/qa/poll", {}))
    step("4. эксперт: итоговый ответ", call("/api/qa/reply", {"case_id": cid, "text": "Да. Подписанное гарантийное письмо заказчика — основание для старта работ до подписания договора во всех дирекциях, включая Техническую. Старт согласует директор дирекции."}))
    step("5. бот разбирает почту → карточка знания", call("/api/qa/poll", {}))
    pr = step("6. карточка (черновик)", call("/api/practices"))
    draft = next((x for x in pr if x["status"] == "draft"), None) if isinstance(pr, list) else None
    if draft:
        step("6a. эксперт подтверждает карточку", call("/api/qa/approve", {"practice_id": draft["practice_id"], "who": "эксперт службы качества"}))
    step("7. кейс 3: повторный вопрос другими словами", call("/api/ask", {"session": "s2", "question": "В Техдирекции клиент прислал гарантийное письмо, договор ещё не подписан. Можно стартовать?", "employee": "petrov@example.test"}))
r8 = step("8. кейс 2: вопрос без подразделения", call("/api/ask", {"session": "s3", "question": "За сколько заранее запускать заявку на расчёт?"}))
opt = next((o["id"] for o in r8.get("options", []) if "ДБРЦТ" in o["label"]), None)
if opt:
    step("9. сотрудник выбирает ДБРЦТ", call("/api/ask", {"session": "s3", "question": "", "choice": opt}))
r10 = step("10. кейс 2: вопрос без подразделения", call("/api/ask", {"session": "s4", "question": "Можно ли начать работы без подписанного договора?"}))
step("11. сотрудник: не знаю", call("/api/ask", {"session": "s4", "question": "", "choice": "?"}))
step("12. вне регламентов", call("/api/ask", {"session": "s5", "question": "Какую маржу поставить по моей сделке?"}))
out = subprocess.run(SSH + ["curl -sS http://127.0.0.1:19652/api/v1/messages?limit=10"], capture_output=True, timeout=60)
mails = json.loads(out.stdout or b"{}").get("messages", [])
print("== письма в имитации почты (Mailpit)")
for m in mails:
    print("  ", m["Created"][:19], m["From"]["Address"], "→", [t["Address"] for t in m["To"]], "копия:", [c["Address"] for c in (m.get("Cc") or [])], "|", m["Subject"][:70])
(RUN / "metrics.json").write_text(json.dumps({"mails": len(mails), "case": cid}, ensure_ascii=False))
