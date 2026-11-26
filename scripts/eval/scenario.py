"""Сквозной сценарий трёх кейсов на развёрнутом ассистенте (стенд, через ssh + curl, тело — stdin)."""
import json
import os
import subprocess
import sys
import time
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
        print("  ", {k: resp.get(k) for k in ("outcome", "text", "case", "seconds", "events", "error", "tokens") if k in resp})
        if resp.get("options"):
            print("     варианты:", [o["label"] for o in resp["options"]])
        for a in resp.get("answers", []):
            print("    ", a["label"], a["verdict"], "|", a["summary"][:220])
            for s in a["steps"]:
                print("       •", s["text"][:150], f"(п. {s['clause']}{', практика' if s.get('practice') else ''})")
    else:
        print("  ", json.dumps(resp, ensure_ascii=False)[:500])
    return resp


step("0. сброс демо-данных", call("/api/admin/reset-demo", {}))
# кейс 1 — вопрос, ответа на который в регламентах нет совсем (решение владельца 26.11; гарантийное письмо — теперь кейс 2, шаг 13)
Q1 = "Сколько лет нужно хранить проектную документацию после перевода проекта в архив?"
r = step("1. кейс 1: вопрос без прямого ответа в регламенте", call("/api/ask", {"session": "s1", "question": Q1, "employee": "ivanov@example.test"}))
if not r.get("case"):
    step("1a. сотрудник: «Уточнить у службы качества»", call("/api/escalate", {"session": "s1"}))
for _ in range(30):                     # черновик для эксперта готовится в фоне, затем уходит письмо
    cases = call("/api/cases")
    if cases and cases[0].get("mail_sent") is not None:
        break
    time.sleep(3)
if cases:
    print("   письмо ушло:", bool(cases[0].get("mail_sent")), "· черновик для эксперта:", json.dumps(cases[0].get("draft"), ensure_ascii=False)[:700])
cid = cases[0]["case_id"] if cases else None
print("   обращение:", cid, cases[0]["status"] if cases else "")
if cid:
    step("2. эксперт: промежуточный ответ", call("/api/qa/reply", {"case_id": cid, "text": "Добрый день! Уточняю у коллег из Технической дирекции, вернусь с ответом."}))
    step("3. бот разбирает почту", call("/api/qa/poll", {}))
    step("4. эксперт: итоговый ответ", call("/api/qa/reply", {"case_id": cid, "text": "Добрый день! Проектную документацию храним 5 лет после перевода проекта в архив — во всех дирекциях. Отвечает за передачу в архив РП."}))
    step("5. бот разбирает почту → карточка знания", call("/api/qa/poll", {}))
    pr = step("6. карточка (черновик)", call("/api/practices"))
    draft = next((x for x in pr if x["status"] == "draft"), None) if isinstance(pr, list) else None
    if draft:
        step("6a. эксперт подтверждает карточку", call("/api/qa/approve", {"practice_id": draft["practice_id"], "who": "эксперт службы качества"}))
    step("7. кейс 3: повторный вопрос другими словами", call("/api/ask", {"session": "s2", "question": "Как долго держим документы проекта, когда он уже в архиве?", "employee": "petrov@example.test"}))
r8 = step("8. кейс 2: вопрос без подразделения", call("/api/ask", {"session": "s3", "question": "За сколько заранее запускать заявку на расчёт?"}))
opt = next((o["id"] for o in r8.get("options", []) if "ДБРЦТ" in o["label"]), None)
if opt:
    step("9. сотрудник выбирает ДБРЦТ", call("/api/ask", {"session": "s3", "question": "", "choice": opt}))
r10 = step("10. кейс 2: вопрос без подразделения", call("/api/ask", {"session": "s4", "question": "Можно ли начать работы без подписанного договора?"}))
step("11. сотрудник: не знаю", call("/api/ask", {"session": "s4", "question": "", "choice": "?"}))
r13 = step("13. кейс 2: ответ зависит от условия (гарантийное письмо, п. 8.4)", call("/api/ask", {"session": "s6", "question": "Техническая дирекция: можно ли начать работы по гарантийному письму заказчика до подписания договора?"}))
if r13.get("outcome") == "ask" and r13.get("options"):
    step("14. сотрудник выбирает первый вариант", call("/api/ask", {"session": "s6", "question": "", "choice": r13["options"][0]["id"]}))
step("15. размытый вопрос", call("/api/ask", {"session": "s7", "question": "А что со сроками?"}))
step("12. вне регламентов", call("/api/ask", {"session": "s5", "question": "Какую маржу поставить по моей сделке?"}))
# 16–19. противоречия: кандидаты → служба качества подтверждает пару → ответ с двумя пунктами → вернуть на решение
pairs = call("/api/conflicts")
print("== 16. реестр противоречий: кандидатов", sum(1 for x in pairs if x.get("status") == "candidate"),
      "подтверждено", sum(1 for x in pairs if x.get("status") == "confirmed"))
pair = next((x for x in pairs if x.get("department") == "DBRCT" and {x.get("clause_a"), x.get("clause_b")} >= {"8.4", "8.5"}), None)
if pair:
    was = pair["status"]
    step("17. служба качества подтверждает ДБРЦТ 8.4↔8.5", call("/api/conflicts/decide", {"pair_id": pair["pair_id"], "decision": "confirm", "who": "сценарий", "comment": "проверка сценария"}))
    step("18. сотрудник: старт без договора по гарантийному письму (ДБРЦТ)", call("/api/ask", {"session": "s8", "question": "ДБРЦТ: работы без подписанного договора и сметы — гарантийное письмо заказчика является исключением?", "employee": "sidorov@example.test"}))
    if was != "confirmed":
        step("19. вернуть пару на решение (подтверждает владелец на видео)", call("/api/conflicts/decide", {"pair_id": pair["pair_id"], "decision": "reopen"}))
else:
    print("   пара ДБРЦТ 8.4↔8.5 среди кандидатов не найдена")
# 20. повторы: тот же вопрос без ответа другими словами от разных сотрудников → сигнал службе качества
for i, q in enumerate(["Сколько лет хранить проектную документацию после закрытия проекта?",
                       "Как долго нужно хранить документы проекта после перевода в архив?"]):
    call("/api/ask", {"session": f"r{i}", "question": q, "employee": f"r{i}@example.test"})
    call("/api/escalate", {"session": f"r{i}"})
time.sleep(5)
print("== 20. повторяющиеся вопросы:", json.dumps((call("/api/analytics").get("repeats") or {}), ensure_ascii=False)[:600])
out = subprocess.run(SSH + ["curl -sS http://127.0.0.1:19652/api/v1/messages?limit=10"], capture_output=True, timeout=60)
mails = json.loads(out.stdout or b"{}").get("messages", [])
print("== письма в имитации почты (Mailpit)")
for m in mails:
    print("  ", m["Created"][:19], m["From"]["Address"], "→", [t["Address"] for t in m["To"]], "копия:", [c["Address"] for c in (m.get("Cc") or [])], "|", m["Subject"][:70])
(RUN / "metrics.json").write_text(json.dumps({"mails": len(mails), "case": cid}, ensure_ascii=False))
