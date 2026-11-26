"""Агент обработки ответов СК: итоговый ли ответ → к каким подразделениям/пунктам → публикация практики.

Решение владельца 26.11 (п. 100 → вариант Б): агент готовит карточку знания, сотрудник службы качества
подтверждает или правит её — только после этого знание попадает в базу.
"""
import json

SYSTEM = ("Ты обрабатываешь письмо эксперта службы качества — ответ на вопрос сотрудника по регламентам проектной "
          "деятельности. Определи по тексту письма: is_final — это итоговый ответ на вопрос (а не «уточняю у коллег», "
          "встречный вопрос, пересылка, благодарность); answer — итоговый ответ коротко, простыми словами, без "
          "добавлений от себя; applies_to — к каким подразделениям применим ответ: список из TECH, IB, DBRCT или "
          "['ALL'], если эксперт говорит, что для всех; question_general — вопрос в общем виде, без частностей "
          "сотрудника (как его зададут другие); variants — 2–4 других формулировки этого вопроса; outcome — practice (разъяснение практики) или "
          "regulation_change (эксперт говорит, что нужно изменить или дополнить регламент). Только из текста письма.")
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["is_final", "answer", "question_general", "variants", "applies_to", "outcome"],
          "properties": {"is_final": {"type": "boolean"}, "answer": {"type": "string", "maxLength": 800},
                         "question_general": {"type": "string", "maxLength": 300},
                         "variants": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 200}},
                         "applies_to": {"type": "array", "items": {"enum": ["TECH", "IB", "DBRCT", "ALL"]}, "maxItems": 3},
                         "outcome": {"enum": ["practice", "regulation_change"]}}}


def process_replies(store, mailbox, llm, link_fn=None, autopublish=False):
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
        qtext = res.get("question_general") or case["question"]
        variants = [v for v in res.get("variants", []) if v.strip()]
        if variants:
            qtext = qtext + "\nДругие формулировки: " + "; ".join(variants)
        pid = store.publish(case["case_id"], r["sender"], qtext, res["answer"], applies, clauses, res["outcome"],
                            status="published" if autopublish else "draft")
        store.set_status(case["case_id"], "published" if autopublish else "answered")
        events.append({"case_id": case["case_id"], "event": "published" if autopublish else "draft",
                       "practice_id": pid, "outcome": res["outcome"]})
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
