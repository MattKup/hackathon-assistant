"""Агентный шаг 1: понять вопрос до поиска.

Модель приводит разговорные/английские слова к терминам регламентов (по словарю config/synonyms.json,
если он есть), выделяет названные подразделения и этап, строит 1–3 поисковых запроса. Без ответа по существу.
"""
import json
from pathlib import Path

SYN = Path(__file__).resolve().parents[2] / "config" / "synonyms.json"
SYSTEM = ("Ты готовишь поиск по регламентам проектной деятельности компании (пресейл, заявка на расчёт, ТКП/КП, "
          "договор с заказчиком и субподрядчиками, смета, старт, ведение, приёмка и закрытие проекта). "
          "Не отвечай на вопрос. Перепиши его так, как это назвал бы регламент: замени разговорные и английские слова "
          "терминами из словаря (если есть), раскрой подразумеваемое («офер» → ТКП/КП, «проджект»/PM → руководитель проекта (РП)). "
          "queries — 1–3 коротких поисковых запроса на русском языке регламента, разные по смыслу, если вопрос составной. "
          "departments — только явно названные в вопросе: TECH (Техническая дирекция, техдирекция), IB (ИБ, информационная "
          "безопасность), DBRCT (ДБРЦТ, ДБР, бизнес-решения); иначе пусто. stage — presale, project или пусто. "
          "vague=true — только если по вопросу нельзя понять, о каком процессе, документе или действии речь "
          "(«а что с договором?», «как быть со сроками?», «а если не успеваем?»); конкретный вопрос — false, даже если ответа может не быть.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["queries", "departments", "stage", "vague"],
          "properties": {"queries": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string", "maxLength": 160}},
                         "departments": {"type": "array", "maxItems": 3, "items": {"enum": ["TECH", "IB", "DBRCT"]}},
                         "stage": {"enum": ["presale", "project", ""]}, "vague": {"type": "boolean"}}}


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


def understand(llm, question):
    global _G
    if _G is None:
        _G = glossary()
    user = (f"Словарь:\n{_G}\n\n" if _G else "") + f"Вопрос сотрудника: {question}"
    try:
        res, meta = llm.chat_json(SYSTEM, user, SCHEMA, max_tokens=300)
        res["queries"] = [q for q in res.get("queries", []) if q.strip()][:3] or [question]
        return res, meta
    except Exception as e:  # шаг не должен ронять ответ
        return {"queries": [question], "departments": [], "stage": "", "error": str(e)[:120]}, {}
