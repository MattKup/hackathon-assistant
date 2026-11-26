"""Почта: отправка обращения в СК (SMTP) и чтение ответов (Mailpit REST API, имитация ящика СК).

Mailpit (документация axllent/mailpit): SMTP :1025, веб и API :8025;
GET api/v1/messages — список (новые сверху); GET api/v1/message/{ID} — письмо; .../headers — заголовки.
В бою — тот же интерфейс поверх IMAP/Exchange. На реальный адрес СК в демо ничего не уходит:
адрес берётся из конфига, по умолчанию — тестовый.
"""
import json
import re
import smtplib
import urllib.request
from email.message import EmailMessage

BOT = "assistant@assistant.local"


def case_email(case, qa_address):
    deps = ", ".join(json.loads(case["departments"])) or "не определено"
    frags = json.loads(case["fragments"])
    lines = [f"Вопрос сотрудника: {case['question']}", f"Подразделение: {deps}", f"Почему бот не ответил: {case['reason']}", ""]
    dlg = json.loads(case["dialogue"])
    if dlg:
        lines.append("Диалог:")
        lines += [f"  {d['role']}: {d['text']}" for d in dlg]
        lines.append("")
    if frags:
        lines.append("Что бот нашёл в регламентах (близкое, но не отвечающее):")
        lines += [f"  • {f['doc']} п. {f['clause']}: {f['text'][:300]}" for f in frags[:5]]
        lines.append("")
    lines.append("Ответьте на это письмо (сотрудник в копии) — итоговый ответ бот добавит в базу знаний как практику.")
    m = EmailMessage()
    m["Subject"] = f"Вопрос по регламентам: {case['question'][:80]}"
    m["From"] = BOT
    m["To"] = qa_address
    if case.get("employee"):
        m["Cc"] = case["employee"]
    m["Message-ID"] = case["message_id"]
    m["X-Case-Id"] = case["case_id"]
    m.set_content("\n".join(lines))
    return m


def send(msg, host, port):
    with smtplib.SMTP(host, port, timeout=10) as s:
        s.send_message(msg)


def reply(case, sender, text, host, port):
    m = EmailMessage()
    m["Subject"] = f"Re: Вопрос по регламентам: {case['question'][:80]}"
    m["From"] = sender
    m["To"] = BOT
    if case.get("employee"):
        m["Cc"] = case["employee"]
    m["In-Reply-To"] = case["message_id"]
    m["References"] = case["message_id"]
    m.set_content(text)
    send(m, host, port)


class Mailpit:
    def __init__(self, api):
        self.api = api.rstrip("/")

    def _get(self, path):
        with urllib.request.urlopen(self.api + path, timeout=10) as r:
            return json.loads(r.read())

    def replies(self, limit=200):
        """Ответы в треды обращений: [{id, case_msgid, sender, text}]."""
        out = []
        for m in self._get(f"/api/v1/messages?limit={limit}").get("messages", []):
            h = self._get(f"/api/v1/message/{m['ID']}/headers")
            ref = " ".join(h.get("In-Reply-To", []) + h.get("References", []))
            found = re.findall(r"<c[0-9a-f]{8}@assistant\.local>", ref, re.I)
            if not found or BOT in " ".join(h.get("From", [])):
                continue
            full = self._get(f"/api/v1/message/{m['ID']}")
            sender = (full.get("From") or {}).get("Address", "")
            out.append({"id": m["ID"], "case_msgid": found[0].lower(), "sender": sender, "text": strip_quoted(full.get("Text", ""))})
        return out


def strip_quoted(text):
    """Убрать цитату исходного письма из ответа."""
    keep = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^(On .+ wrote:|.+ пишет:|-----Original Message-----)", line.strip()):
            break
        keep.append(line)
    return "\n".join(keep).strip()
