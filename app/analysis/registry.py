"""Реестр противоречий между пунктами регламентов (SQLite, та же база, что обращения СК).

Поток:
  анализатор (ночью, app/analysis/conflicts.py) → кандидаты: пара пунктов, дословные цитаты, отпечатки текста, объяснение
  → служба качества на странице «Противоречия»: «Подтвердить» / «Не противоречие» + комментарий (кто, когда)
  → ассистент применяет только подтверждённые пары, у которых текст обоих пунктов не изменился (отпечаток),
    и только когда второй пункт пары по теме вопроса (app/pipeline.py: load_conflicts, apply_conflicts).

Повторный ночной прогон не плодит дубли и не сбрасывает решения: ключ пары — документ+пункт обеих сторон и
отпечатки их текста. Изменился текст пункта — это новая пара-кандидат (прежнее подтверждение к ней не относится).
"""
import hashlib
import json
import re
import sqlite3
import time

KINDS_FOR_REVIEW = ("contradiction", "possible")     # различия дирекций и зависимости — не противоречия, СК их не подтверждает

SCHEMA = """
create table if not exists conflict_pairs(
  pair_id text primary key, created real, source text, kind text, relation text, department text,
  doc_a text, title_a text, clause_a text, quote_a text, hash_a text,
  doc_b text, title_b text, clause_b text, quote_b text, hash_b text,
  situation text, explanation text,
  status text, decided_by text, decided_at real, comment text);
"""
STATUSES = ("candidate", "confirmed", "rejected")


def text_hash(text):
    """Отпечаток текста пункта (12 знаков sha1 от нормализованных первых 600 знаков)."""
    return hashlib.sha1(re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()[:600].encode()).hexdigest()[:12]


def clause_texts(windows):
    """(doc_id, пункт) → список текстов окон пункта (длинный пункт может быть в нескольких окнах)."""
    out = {}
    for w in windows:
        out.setdefault((w.get("doc_id"), w.get("clause")), []).append(w)
    return out


def _title(w):
    return w.get("doc_title") or w.get("path", "").split(" › ")[0]


def _pair_id(doc_a, clause_a, hash_a, doc_b, clause_b, hash_b):
    sides = sorted([(doc_a, clause_a, hash_a), (doc_b, clause_b, hash_b)])
    return "K" + hashlib.sha1(json.dumps(sides).encode()).hexdigest()[:10].upper()


class ConflictRegistry:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")      # ночной анализатор пишет кандидатов, пока сервер работает
        self.db.executescript(SCHEMA)

    # --- кандидаты ---
    def add_candidate(self, side_a, side_b, *, source, kind="contradiction", relation="", situation="", explanation=""):
        """side = {doc_id, doc_title, clause, quote, text}. Возвращает (pair_id, новая ли)."""
        ha, hb = text_hash(side_a["text"]), text_hash(side_b["text"])
        pid = _pair_id(side_a["doc_id"], side_a["clause"], ha, side_b["doc_id"], side_b["clause"], hb)
        if self.db.execute("select 1 from conflict_pairs where pair_id=?", (pid,)).fetchone():
            return pid, False
        self.db.execute("insert into conflict_pairs values(?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?, ?,?,?,?)",
                        (pid, time.time(), source, kind, relation, side_a.get("department", ""),
                         side_a["doc_id"], side_a.get("doc_title", ""), side_a["clause"], side_a["quote"], ha,
                         side_b["doc_id"], side_b.get("doc_title", ""), side_b["clause"], side_b["quote"], hb,
                         situation, explanation, "candidate", None, None, None))
        self.db.commit()
        return pid, True

    def import_report(self, report, windows, source="анализатор противоречий"):
        """Находки анализатора (conflicts.json) → кандидаты. Цитаты анализатор уже сверил с текстом; отпечаток —
        по текущему тексту пункта в индексе (окна пункта склеиваются)."""
        texts = clause_texts(windows)
        added = skipped = missing = 0
        for f in report.get("findings", []):
            if f.get("kind") not in KINDS_FOR_REVIEW:
                skipped += 1
                continue
            sides = []
            for s in (f["a"], f["b"]):
                ws = texts.get((s["doc_id"], s["clause"]))
                if not ws:
                    break
                sides.append({**s, "text": " ".join(w["text"] for w in ws), "doc_title": s.get("doc_title") or _title(ws[0])})
            if len(sides) < 2:
                missing += 1                       # пункта больше нет в базе — кандидат неактуален
                continue
            _, new = self.add_candidate(*sides, source=source, kind=f["kind"], relation=f.get("relation", ""),
                                        situation=f.get("situation", ""), explanation=f.get("explanation", ""))
            added += new
        return {"added": added, "skipped_kinds": skipped, "clause_missing": missing}

    def import_manual(self, pairs, windows, source):
        """Пары, найденные человеком (department, a, b, why[, doc]) → кандидаты; цитата — полный текст пункта."""
        texts = clause_texts(windows)
        added = 0
        for p in pairs:
            docs = {d for (d, cl) in texts if cl == p["a"] and (d, p["b"]) in texts
                    and texts[(d, cl)][0].get("department") == p["department"]
                    and (not p.get("doc") or p["doc"].lower() in texts[(d, cl)][0].get("path", "").lower())}
            for d in sorted(docs):
                sides = []
                for cl in (p["a"], p["b"]):
                    ws = texts[(d, cl)]
                    t = " ".join(w["text"] for w in ws)
                    sides.append({"doc_id": d, "doc_title": _title(ws[0]), "clause": cl, "quote": t,
                                  "text": t, "department": p["department"]})
                _, new = self.add_candidate(*sides, source=source, explanation=p.get("why", ""))
                added += new
        return {"added": added}

    # --- решения службы качества ---
    def decide(self, pair_id, decision, who, comment=""):
        status = {"confirm": "confirmed", "reject": "rejected", "reopen": "candidate"}[decision]
        cur = self.db.execute("update conflict_pairs set status=?, decided_by=?, decided_at=?, comment=? where pair_id=?",
                              (status, who if status != "candidate" else None, time.time() if status != "candidate" else None,
                               comment or None, pair_id))
        self.db.commit()
        if not cur.rowcount:
            raise KeyError(pair_id)

    def pairs(self, status=None, windows=None):
        q, args = "select * from conflict_pairs", ()
        if status:
            q, args = q + " where status=?", (status,)
        rows = [dict(r) for r in self.db.execute(q + " order by created desc", args)]
        if windows is not None:                   # актуальность: текст обоих пунктов тот же, что при находке
            texts = clause_texts(windows)
            for r in rows:
                r["current"] = all(
                    (d, c) in texts and text_hash(" ".join(w["text"] for w in texts[(d, c)])) == h
                    for d, c, h in ((r["doc_a"], r["clause_a"], r["hash_a"]), (r["doc_b"], r["clause_b"], r["hash_b"])))
        return rows

    def confirmed(self):
        return self.pairs("confirmed")
