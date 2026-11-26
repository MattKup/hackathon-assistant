"""Г14: ночные «входы» к пунктам — вопросы, как их зададут сотрудники (doc2query / индексация гипотетических вопросов).

Ночью модель по тексту каждого пункта пишет несколько вопросов (своими словами, разговорно, жаргоном, через ситуацию).
Вопросы попадают в поисковый индекс как дополнительные записи, указывающие на пункт: живой вопрос, сказанный чужими
словами, находит пункт через похожий сгенерированный вопрос. Ответ по-прежнему строится только из текста пункта
(с дословной цитатой и проверками кодом) — сгенерированные вопросы в ответ не попадают.
"""
import json

SYSTEM = ("Ты помогаешь построить поиск по внутренним регламентам компании. По тексту одного пункта регламента напиши вопросы, "
          "которые на него задал бы сотрудник в чате, — так, как спрашивают живые люди: своими словами, без терминов из пункта, "
          "через рабочую ситуацию («клиент торопит…», «шеф разрешил устно…»), разговорными словами и рабочим жаргоном "
          "(офер, кикофф, сабы, проджект, смета), коротко. Каждый вопрос должен иметь ответ именно в этом пункте. "
          "Не придумывай фактов, которых нет в пункте; если пункт — заголовок или в нём нечего спросить, верни пустой список.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["questions"],
          "properties": {"questions": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 200}}}}


def generate(llm, w):
    user = f"Подразделение: {w.get('department_name') or w.get('department')}\nДокумент и раздел: {w.get('path', '')}\n\nТекст пункта:\n{w['text']}"
    js, meta = llm.chat_json(SYSTEM, user, SCHEMA, max_tokens=500)
    return [q.strip() for q in js.get("questions", []) if q.strip()], meta


def load(path):
    """questions.jsonl → {window_id: [вопросы]}"""
    out = {}
    for line in open(path):
        if line.strip():
            r = json.loads(line)
            out[r["window_id"]] = r.get("questions") or []
    return out


def augment(windows, qmap):
    """Окна + записи-вопросы (kind=qmap, parent=window_id пункта) для индекса поиска."""
    extra = []
    for w in windows:
        for i, q in enumerate(qmap.get(w["window_id"]) or []):
            extra.append({**{k: w.get(k) for k in ("doc_id", "department", "stage", "clause", "section", "section_title", "path")},
                          "window_id": f"{w['window_id']}#q{i}", "text": q, "kind": "qmap", "parent": w["window_id"],
                          "external_refs": []})
    return windows + extra


def collapse(hits, by_id, k):
    """Попадания по вопросам → пункт-родитель (с наибольшим баллом); через какой вопрос нашли — в поле via_q."""
    best = {}
    for h in hits:
        if h.get("kind") == "qmap":
            p = by_id.get(h["parent"])
            if not p:
                continue
            h2 = {**p, "score": h["score"], "via_q": h["text"]}
        else:
            h2 = h
        cur = best.get(h2["window_id"])
        if cur is None or h2["score"] > cur["score"]:
            best[h2["window_id"]] = h2
    return sorted(best.values(), key=lambda h: -h["score"])[:k]
