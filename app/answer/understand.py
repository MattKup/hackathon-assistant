"""Агентный шаг 1: понять вопрос до поиска.

Модель приводит разговорные/английские слова к терминам регламентов (по словарю config/synonyms.json,
если он есть), выделяет названные подразделения и этап, строит 1–3 поисковых запроса, определяет тип вопроса
(intent) и размытость (vague). Без ответа по существу. Подразделения и предметная область — из конфигурации
(app/config.py): новый документ нового подразделения работает без правки кода.
"""
import json
from pathlib import Path

from app.config import dept_ids, domain, titles

SYN = Path(__file__).resolve().parents[2] / "config" / "synonyms.json"
INTENTS = ["question", "find_document", "task", "out_of_scope"]
MAX_LISTED_DEPTS = 30      # больше — подразделения распознаются только по названиям (app/dialog/clarify.mentioned), промпт не раздувается


def system_prompt(registry):
    t = titles(registry)
    ids = dept_ids(registry)
    deps = "; ".join(f"{d} ({t.get(d, d)})" for d in ids) if len(ids) <= MAX_LISTED_DEPTS else ""
    return ("Ты готовишь поиск по базе знаний компании: " + domain() + ". "
            "Не отвечай на вопрос. Перепиши его так, как это назвал бы регламент: замени разговорные и английские слова "
            "терминами из словаря (если есть), раскрой подразумеваемое («офер» → ТКП/КП, «проджект»/PM → руководитель проекта (РП)). "
            "queries — 1–3 коротких поисковых запроса на русском языке регламента, разные по смыслу, если вопрос составной. "
            + (f"departments — только явно названные в вопросе подразделения из списка: {deps}; иначе пусто. " if deps
               else "departments — всегда пусто. ") +
            "stage — presale, project или пусто. "
            "intent: question — вопрос о правилах и процессах (в том числе если ответа может не быть); "
            "find_document — сотрудник спрашивает, В КАКОМ документе или разделе что-то описано, где это найти; "
            "task — просит выполнить работу за него (написать, составить, посчитать, отправить, проанализировать его материалы); "
            "out_of_scope — вопрос не о процессах компании: экономика его конкретной сделки (какую маржу поставить, какая будет "
            "премия), текущие данные (сколько денег осталось сейчас), рынок, конкуренты, законы вообще, бытовое. "
            "vague=true — только если по вопросу нельзя понять, о каком процессе, документе или действии речь "
            "(«а что с договором?», «как быть со сроками?»); конкретный вопрос — false, даже если ответа может не быть.")


def schema(registry):
    return {"type": "object", "additionalProperties": False, "required": ["queries", "departments", "stage", "vague", "intent"],
            "properties": {"queries": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string", "maxLength": 160}},
                           "departments": {"type": "array", "maxItems": 5 if len(dept_ids(registry)) <= MAX_LISTED_DEPTS else 0,
                                           "items": {"enum": dept_ids(registry)[:MAX_LISTED_DEPTS] or ["NONE"]}},
                           "stage": {"enum": ["presale", "project", ""]}, "vague": {"type": "boolean"},
                           "intent": {"enum": INTENTS}}}


def glossary():
    if not SYN.exists():
        return ""
    js = json.loads(SYN.read_text())
    lines = []
    for k, v in (js.get("abbreviations") or {}).items():
        exp = v.get("expansions") if isinstance(v, dict) else v
        lines.append(f"{k} = {', '.join(exp[:2])}")
    for k, v in (js.get("colloquial") or {}).items():
        term = v.get("term") if isinstance(v, dict) else v
        lines.append(f"{k} → {term}")
    return "\n".join(lines[:120])


_G = None


def understand(llm, question, registry=()):
    global _G
    if _G is None:
        _G = glossary()
    registry = list(registry)
    user = (f"Словарь:\n{_G}\n\n" if _G else "") + f"Вопрос сотрудника: {question}"
    try:
        res, meta = llm.chat_json(system_prompt(registry), user, schema(registry), max_tokens=300)
        res["queries"] = [q for q in res.get("queries", []) if q.strip()][:3] or [question]
        res.setdefault("intent", "question")
        return res, meta
    except Exception as e:  # шаг не должен ронять ответ
        return {"queries": [question], "departments": [], "stage": "", "intent": "", "error": str(e)[:120]}, {}
