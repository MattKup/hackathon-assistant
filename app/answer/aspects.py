"""«Обратите внимание — не из регламента» (дополнение организаторов 27.11, необязательная часть задания).

Полный ответ = ответ по регламенту + совет обратить внимание на внешние аспекты случая (Гражданский кодекс,
лицензируемые виды деятельности, особые виды заказчиков). Внутреннее наружу не уходит: ничего не запрашивается
в интернете. Источники советов — только то, что задала Служба качества:
  1) правила «слова-признаки → совет» (настройка `aspects`, правится в вебе «Настройки»);
  2) внешние документы (закон, справка), загруженные отдельно с типом «Внешний источник» — подразделение EXTERNAL.
Модель здесь не участвует: совет показывается дословно, отдельным блоком и никогда не смешивается с ответом
по регламенту. Внешние документы не участвуют в основном поиске и в ответе по регламенту.
"""
import re

EXTERNAL = "EXTERNAL"
EXTERNAL_NAME = "Внешние источники"
SHOW_FOR = {"answer", "answer_with_caveat", "conflict", "escalate"}


def is_external(doc):
    return doc.get("department") == EXTERNAL


def _stems(text):
    return [w[:max(4, len(w) - 2)] for w in re.findall(r"[a-zа-яё0-9-]+", (text or "").lower().replace("ё", "е")) if len(w) > 2]


def parse_rules(lines):
    """Строки «слово, слово → совет» (или «: ») → [{"when": [...], "note": "..."}]."""
    out = []
    for ln in lines or []:
        if isinstance(ln, dict):
            when, note = ln.get("when") or [], (ln.get("note") or "").strip()
            when = [w.strip() for w in (when.split(",") if isinstance(when, str) else when) if str(w).strip()]
        else:
            m = re.split(r"\s*(?:→|->|=>)\s*", str(ln), maxsplit=1)
            if len(m) != 2:
                continue
            when, note = [w.strip() for w in m[0].split(",") if w.strip()], m[1].strip()
        if when and note:
            out.append({"when": when[:20], "note": note[:500]})
    return out[:40]


def rules_to_lines(rules):
    return [", ".join(r["when"]) + " → " + r["note"] for r in parse_rules(rules)]


def match_rules(question, rules):
    """Правило срабатывает, если в вопросе есть слово-признак (по основе: «госзаказчик» ~ «госзаказчика»)."""
    q = " " + " ".join(re.findall(r"[a-zа-яё0-9-]+", (question or "").lower().replace("ё", "е"))) + " "
    hits = []
    for r in parse_rules(rules):
        for w in r["when"]:
            st = _stems(w)                           # каждое слово признака — началом слова вопроса (формы слов: «защите информации»)
            if st and all((" " + x) in q for x in st):
                hits.append({"kind": "rule", "note": r["note"], "matched": w})
                break
    return hits[:3]


def external_hits(search, question, ext_ids, k=2, min_score=0.3):
    """Фрагменты внешних документов по вопросу — только из подразделения EXTERNAL, порог по скору реранкера."""
    if not ext_ids:
        return []
    out = []
    for h in search(question, set(ext_ids), k) or []:
        if h.get("score", 0) < min_score:
            continue
        out.append({"kind": "doc", "doc_id": h.get("doc_id"), "title": (h.get("path") or "").split(" › ")[0],
                    "text": (h.get("text") or "")[:400], "clause": h.get("clause") or ""})
    return out
