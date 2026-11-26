"""Агент обработки ответов СК: итоговый ли ответ → к каким подразделениям/пунктам → публикация практики.

Решение владельца по публикации (п. 100): ⏳; по умолчанию — сразу с возможностью отзыва (рекомендация).
"""
import json

SYSTEM = ("Ты обрабатываешь письмо эксперта службы качества — ответ на вопрос сотрудника по регламентам проектной "
          "деятельности. Определи по тексту письма: is_final — это итоговый ответ на вопрос (а не «уточняю у коллег», "
          "встречный вопрос, пересылка, благодарность); answer — итоговый ответ коротко, простыми словами, без "
          "добавлений от себя; applies_to — к каким подразделениям применим ответ: список из TECH, IB, DBRCT или "
          "['ALL'], если эксперт говорит, что для всех; outcome — practice (разъяснение практики) или "
          "regulation_change (эксперт говорит, что нужно изменить или дополнить регламент). Только из текста письма.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["is_final", "answer", "applies_to", "outcome"],
          "properties": {"is_final": {"type": "boolean"}, "answer": {"type": "string", "maxLength": 800},
                         "applies_to": {"type": "array", "items": {"enum": ["TECH", "IB", "DBRCT", "ALL"]}, "maxItems": 3},
                         "outcome": {"enum": ["practice", "regulation_change"]}}}


def process_replies(store, mailbox, llm, link_fn=None, autopublish=True):
    """link_fn(question, answer, applies_to) -> [{doc, clause, window_id}] — пункты регламента рядом с практикой."""
    by_msgid = {c["message_id"]: c for c in store.cases()}
    events = []
    for r in mailbox.replies():
        if store.seen_mail(r["id"]) or r["case_msgid"] not in by_msgid:
            continue
        case = by_msgid[r["case_msgid"]]
        user = f"Вопрос сотрудника: {case['question']}\n\nПисьмо эксперта ({r['sender']}):\n{r['text']}"
        res, _ = llm.chat_json(SYSTEM, user, SCHEMA, max_tokens=500)
        store.add_mail(r["id"], case["case_id"], "in", r["sender"], r["text"], "final" if res["is_final"] else "interim")
        if not res["is_final"]:
            store.set_status(case["case_id"], "in_work")
            events.append({"case_id": case["case_id"], "event": "interim"})
            continue
        applies = ["TECH", "IB", "DBRCT"] if "ALL" in res["applies_to"] or not res["applies_to"] else res["applies_to"]
        clauses = link_fn(case["question"], res["answer"], applies) if link_fn else []
        if autopublish:
            pid = store.publish(case["case_id"], r["sender"], case["question"], res["answer"], applies, clauses, res["outcome"])
            store.set_status(case["case_id"], "published")
            events.append({"case_id": case["case_id"], "event": "published", "practice_id": pid, "outcome": res["outcome"]})
        else:
            store.set_status(case["case_id"], "answered")
            events.append({"case_id": case["case_id"], "event": "awaiting_approval"})
    return events


def practice_windows(store):
    """Практики как окна поиска (kind=practice): по одному окну на подразделение области действия."""
    out = []
    for p in store.practices():
        for dep in p["applies_to"]:
            out.append({"window_id": f"{p['practice_id']}-{dep}", "doc_id": f"practice:{dep}", "department": dep,
                        "stage": "practice", "section": "П", "section_title": "Практика службы качества",
                        "clause": p["practice_id"], "kind": "practice",
                        "path": f"Практика службы качества › {p['practice_id']}",
                        "text": f"Вопрос: {p['question']}\nОтвет службы качества: {p['answer']}",
                        "practice": {k: p[k] for k in ("practice_id", "expert", "created", "clauses", "outcome")}})
    return out
