"""Мониторинг повторов: «этот вопрос задавали N раз за период» → сигнал службе качества и в аналитику.

Письма не склеиваются: каждое обращение уходит как раньше (дубль того же сотрудника за 2 ч — не уходит). Отдельно
считается, сколько раз за период в службу качества передавали тот же вопрос — дословно или другими словами
(похожесть — косинус эмбеддингов bge-m3 на стенде, без модели — доля общих основ слов). Порог достигнут → одно
письмо-сигнал СК на вопрос за период и строка в аналитике. Период, порог и похожесть — config/documents.json
«repeat_monitor», без кода.
"""
import json
import time
import uuid

from app.answer.core import stems

DEFAULTS = {"period_days": 7, "threshold": 3, "similarity": 0.8, "similarity_words": 0.6}

SCHEMA = """
create table if not exists escalation_log(
  t real, question text, employee text, case_id text, reason text);
create table if not exists repeat_signals(
  signal_id text primary key, created real, question text, count integer, period_days integer,
  variants text, case_ids text, mail_sent integer default 0);
"""


def settings():
    from app.config import cfg
    return {**DEFAULTS, **(cfg().get("repeat_monitor") or {})}


def words_similar(threshold):
    """Без модели эмбеддингов: доля общих основ от меньшего вопроса."""
    def sim(a, b):
        sa, sb = stems(a), stems(b)
        return bool(sa and sb) and len(sa & sb) / min(len(sa), len(sb)) >= threshold
    return sim


def vectors_similar(embed, threshold):
    """С моделью эмбеддингов: косинус ≥ порога; вектор вопроса считается один раз."""
    cache = {}

    def vec(q):
        if q not in cache:
            v = embed([q])[0]
            n = sum(x * x for x in v) ** .5 or 1
            cache[q] = [x / n for x in v]
        return cache[q]

    def sim(a, b):
        return sum(x * y for x, y in zip(vec(a), vec(b))) >= threshold
    return sim


class Repeats:
    def __init__(self, db, similar, conf=None):
        self.db, self.similar = db, similar
        self.conf = conf or settings()
        self.db.executescript(SCHEMA)

    def _period(self, now):
        return now - self.conf["period_days"] * 86400

    def record(self, question, employee, case_id, reason, now=None):
        """Учесть передачу вопроса в СК. Возвращает {count, signal} — signal, если порог достигнут впервые за период."""
        now = now or time.time()
        self.db.execute("insert into escalation_log values(?,?,?,?,?)", (now, question, employee, case_id, reason))
        self.db.commit()
        rows = self.db.execute("select question, case_id from escalation_log where t>=? order by t", (self._period(now),)).fetchall()
        same = [r for r in rows if r[0] == question or self.similar(question, r[0])]
        count = len(same)
        if count < self.conf["threshold"]:
            return {"count": count, "signal": None}
        for s in self.db.execute("select signal_id, question from repeat_signals where created>=?", (self._period(now),)).fetchall():
            if s[1] == question or self.similar(question, s[1]):
                self.db.execute("update repeat_signals set count=? where signal_id=?", (count, s[0]))
                self.db.commit()
                return {"count": count, "signal": None}            # сигнал за этот период уже был — не повторяем
        sid = "R" + uuid.uuid4().hex[:8].upper()
        variants = list(dict.fromkeys(r[0] for r in same))
        cases = list(dict.fromkeys(r[1] for r in same if r[1]))
        self.db.execute("insert into repeat_signals values(?,?,?,?,?,?,?,0)",
                        (sid, now, question, count, self.conf["period_days"], json.dumps(variants, ensure_ascii=False),
                         json.dumps(cases)))
        self.db.commit()
        return {"count": count, "signal": {"signal_id": sid, "question": question, "count": count,
                                           "period_days": self.conf["period_days"], "variants": variants, "case_ids": cases}}

    def mark_sent(self, signal_id):
        self.db.execute("update repeat_signals set mail_sent=1 where signal_id=?", (signal_id,))
        self.db.commit()

    def groups(self, now=None):
        """Для аналитики: вопросы, переданные в СК за период, сгруппированные по похожести (≥ 2 раз)."""
        now = now or time.time()
        rows = self.db.execute("select question, case_id from escalation_log where t>=? order by t", (self._period(now),)).fetchall()
        gs = []
        for q, cid in rows:
            g = next((g for g in gs if q == g["question"] or self.similar(q, g["question"])), None)
            if g is None:
                gs.append({"question": q, "count": 1, "variants": [q], "case_ids": [cid] if cid else []})
            else:
                g["count"] += 1
                if q not in g["variants"]:
                    g["variants"].append(q)
                if cid and cid not in g["case_ids"]:
                    g["case_ids"].append(cid)
        signaled = [s[0] for s in self.db.execute("select question from repeat_signals where created>=?", (self._period(now),))]
        for g in gs:
            g["signal"] = any(g["question"] == s or self.similar(g["question"], s) for s in signaled)
        return sorted([g for g in gs if g["count"] >= 2], key=lambda g: -g["count"])


def signal_email(sig, qa_address):
    from email.message import EmailMessage
    from app.escalation.mail import BOT
    lines = [f"Этот вопрос передавали в службу качества {sig['count']} раз за {sig['period_days']} дн.", "",
             "Формулировки сотрудников:"] + [f"  • {v}" for v in sig["variants"][:10]] + [
             "", f"Обращения: {', '.join(sig['case_ids']) or '—'}", "",
             "Ответ на любое из обращений станет карточкой знания — повторные вопросы помощник закроет сам.",
             "Если тема регулярная — возможно, её стоит добавить в регламент."]
    m = EmailMessage()
    m["Subject"] = f"Повторяющийся вопрос ({sig['count']} раз за {sig['period_days']} дн.): {sig['question'][:70]}"
    m["From"] = BOT
    m["To"] = qa_address
    m["X-Repeat-Signal"] = sig["signal_id"]
    m.set_content("\n".join(lines))
    return m
