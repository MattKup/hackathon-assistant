"""Уточнение подразделения/стадии правилом по выдаче (без LLM).

Решение по вопросу:
- в вопросе или в сессии названы документ(ы) → фильтр по ним, без уточнения;
- иначе, если среди лучших окон есть ≥ 2 документа с близкими скорами → `clarify`
  с вариантами = эти документы (в порядке лучшего скора) + «несколько» + «не знаю»;
- иначе — отвечаем по единственному документу.
Бюджет уточнений на сессию — 2 (жёсткий предел 3) — считает вызывающий код.
"""
import re
from dataclasses import dataclass, field

# Упоминания подразделений — из config/documents.json (department_aliases), новые подразделения — по названию.
from app.config import aliases as _aliases

DEPT_ALIASES = _aliases([])
PRESALE = [r"пресейл\w*", r"presale", r"до продажи", r"на этапе продажи"]
PROJECT = [r"в проекте", r"на проекте", r"реализаци\w* проекта", r"исполнени\w*"]


@dataclass
class Decision:
    action: str                      # "answer" | "clarify"
    doc_ids: list = field(default_factory=list)
    options: list = field(default_factory=list)   # [{doc_id, department, stage, label}]
    reason: str = ""


def mentioned(question, registry):
    """Документы, которые вопрос явно называет (по подразделению и стадии)."""
    q = question.lower()
    depts = {d for d, pats in _aliases(registry).items() if any(re.search(p, q) for p in pats)}
    presale = any(re.search(p, q) for p in PRESALE)
    project = any(re.search(p, q) for p in PROJECT)
    if not depts:
        return []
    out = []
    for doc in registry:
        if doc["department"] not in depts:
            continue
        same_dept = [d for d in registry if d["department"] == doc["department"]]
        if len(same_dept) > 1:                       # у ИБ два документа — нужна стадия
            if presale and not project and doc["stage"] != "presale":
                continue
            if project and not presale and doc["stage"] == "presale":
                continue
        out.append(doc["doc_id"])
    return out


def label(doc, registry):
    """Короткая подпись документа: подразделение (+ стадия, если у подразделения несколько документов)."""
    same = [d for d in registry if d["department"] == doc["department"]]
    name = doc.get("department_name") or doc["department"]
    if len(same) > 1:
        stages = [d["stage"] for d in same]
        if doc["stage"] in ("presale", "project") and stages.count(doc["stage"]) == 1:
            name += " — " + {"presale": "пресейл", "project": "проект"}[doc["stage"]]
        else:
            name += " — " + doc.get("display_title", "")[:60]
    return name


def decide(question, hits, registry, session_docs=None, top=10, ratio=0.2, floor=0.12):
    named = mentioned(question, registry)
    if named:
        return Decision("answer", named, reason="подразделение названо в вопросе")
    if session_docs:
        return Decision("answer", list(session_docs), reason="подразделение выбрано ранее в диалоге")
    if not hits:
        return Decision("answer", [], reason="ничего не найдено")
    best = hits[0]["score"]
    seen = []
    for h in hits[:top]:
        if h["score"] >= max(floor, ratio * best) and h["doc_id"] not in seen:
            seen.append(h["doc_id"])
    if len(seen) >= 2:
        by_id = {d["doc_id"]: d for d in registry}
        opts = [{"doc_id": i, "department": by_id[i]["department"], "stage": by_id[i]["stage"],
                 "label": label(by_id[i], registry)} for i in seen]
        return Decision("clarify", seen, opts, reason=f"найдено {len(seen)} подразделения/стадии с близкими ответами")
    return Decision("answer", seen, reason="ответ в одном документе")
