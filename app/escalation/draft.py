"""Черновик ответа для службы качества — второй проход LLM по обращению (идея владельца 26.11).

Обращения копятся в базе (cases). По каждому новому обращению отдельная модель (LLM_REVIEW_*; по умолчанию —
та же, что отвечает; можно направить на более крупную open-weights модель) готовит эксперту черновик:
что в регламентах есть по теме (с пунктами), чего не хватает, и предложение ответа — явно помеченное как
предложение, требующее подтверждения. Сотруднику черновик не показывается; в базу знаний попадает только
ответ, подтверждённый экспертом (кейс 3).
"""
import json
import time

SYSTEM = ("Ты помогаешь эксперту службы качества ответить на вопрос сотрудника о регламентах проектной деятельности. "
          "Ассистент не смог ответить однозначно. Ниже — вопрос, диалог и найденные фрагменты регламентов. "
          "Подготовь черновик для эксперта (не для сотрудника): found — что по теме вопроса в регламентах ЕСТЬ, "
          "с номерами пунктов, только по фрагментам; gap — чего именно в регламентах нет или что не оговорено; "
          "proposal — предложение ответа сотруднику, которое эксперт может подтвердить или исправить: что следует из "
          "регламента — со ссылкой на пункт, что требует решения эксперта — прямо пометь «требует решения эксперта». "
          "Не выдавай своё предложение за правило регламента. clauses — номера использованных пунктов. "
          "Сокращения не расшифровывай сам. Пиши кратко, на «вы» к эксперту не обязательно, без канцелярита.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["found", "gap", "proposal", "clauses"],
          "properties": {"found": {"type": "string", "maxLength": 700}, "gap": {"type": "string", "maxLength": 400},
                         "proposal": {"type": "string", "maxLength": 700},
                         "clauses": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 20}}}}


def prepare(llm, case, model_name=""):
    frags = json.loads(case["fragments"] or "[]")
    dlg = json.loads(case["dialogue"] or "[]")
    user = (f"Вопрос сотрудника: {case['question']}\nПочему ассистент не ответил: {case['reason']}\n"
            + ("Диалог:\n" + "\n".join(f"  {d['role']}: {d['text']}" for d in dlg) + "\n" if dlg else "")
            + "\nФрагменты регламентов:\n\n"
            + "\n\n".join(f"[{f['doc']} · п. {f['clause']}]\n{f['text']}" for f in frags[:8]))
    t0 = time.time()
    res, meta = llm.chat_json(SYSTEM, user, SCHEMA, max_tokens=700)
    return {**res, "model": model_name, "seconds": round(time.time() - t0, 2),
            "prompt_tokens": (meta or {}).get("prompt_tokens"), "completion_tokens": (meta or {}).get("completion_tokens")}


def as_text(d):
    if not d:
        return []
    out = ["Черновик ответа (подготовил ассистент; требует подтверждения эксперта):"]
    if d.get("found"):
        out.append("  В регламентах есть: " + d["found"])
    if d.get("gap"):
        out.append("  Не оговорено: " + d["gap"])
    if d.get("proposal"):
        out.append("  Предложение ответа: " + d["proposal"])
    return out + [""]
