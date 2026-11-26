"""Ответ по найденным окнам + проверки кодом.

Проверки (без второго вызова LLM): ссылка ref существует; quote — дословная подстрока окна
(после нормализации пробелов/кавычек); шаг опирается на окно своего подразделения;
числа шага есть в цитате окна. Шаг, не прошедший проверку, убирается и попадает в `rejected`.
Если после проверки не осталось опоры — verdict понижается до no_answer.
"""
import json
import re
from pathlib import Path

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
SYSTEM = (PROMPTS / "system_answer.md").read_text()
SCHEMA = json.loads((PROMPTS / "answer.schema.json").read_text())
NUM = re.compile(r"\d+(?:[.,]\d+)?")


def norm(s):
    s = s.lower().replace("ё", "е").replace("«", '"').replace("»", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip(" .;:")


def build_user(question, windows, dept_label):
    parts = []
    for i, w in enumerate(windows, 1):
        parts.append(f"[w{i}] {w['department_name']} · {w['doc_title']} · п. {w['clause']} · раздел «{w['section_title']}»\n{w['text']}")
    return f"Фрагменты регламентов:\n\n" + "\n\n".join(parts) + f"\n\nПодразделение: {dept_label}\n\nВопрос: {question}"


def _sentences(text):
    return [s for s in re.split(r"(?<=[.;:!?])\s+|\n+", text) if s.strip()]


def repair_quote(quote, text, min_overlap=0.8):
    """Цитата почти дословная → вернуть предложение источника, где совпадает ≥ 80 % слов цитаты."""
    qt = [w for w in re.findall(r"\w+", norm(quote)) if len(w) > 2]
    if not qt:
        return None
    best, score = None, 0.0
    for s in _sentences(text):
        st = set(re.findall(r"\w+", norm(s)))
        sc = sum(1 for w in qt if w in st) / len(qt)
        if sc > score:
            best, score = s.strip(), sc
    return best if score >= min_overlap else None


def check(raw, windows):
    out = {**raw, "steps": [], "rejected": []}
    for st in raw.get("steps", []):
        m = re.fullmatch(r"w(\d+)", st.get("ref", ""))
        reason = None
        if not m or not (1 <= int(m.group(1)) <= len(windows)):
            reason = "ref_missing"
        else:
            w = windows[int(m.group(1)) - 1]
            if st.get("quote") and norm(st["quote"]) not in norm(w["text"]):
                fixed = repair_quote(st["quote"], w["text"])
                if fixed:
                    st = {**st, "quote": fixed, "quote_repaired": True}
            if not st.get("quote") or norm(st["quote"]) not in norm(w["text"]):
                reason = "quote_not_verbatim"
            else:
                nums_step = set(NUM.findall(st.get("text", "") + " " + st.get("deadline", "")))
                if nums_step - set(NUM.findall(w["text"])):
                    reason = "number_not_in_source"
        if reason:
            out["rejected"].append({**st, "reason": reason})
        else:
            w = windows[int(st["ref"][1:]) - 1]
            out["steps"].append({**st, "window_id": w["window_id"], "clause": w["clause"],
                                 "department": w["department"], "doc_title": w["doc_title"]})
    if raw.get("verdict") in ("answer", "answer_with_caveat", "conflict") and raw.get("steps") and not out["steps"]:
        out["verdict"] = "no_answer"
        out["downgraded"] = "все шаги без проверенной опоры"
    return out


def answer(llm, question, windows, dept_label):
    raw, meta = llm.chat_json(SYSTEM, build_user(question, windows, dept_label), SCHEMA)
    return check(raw, windows), meta
