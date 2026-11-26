"""Обращения в службу качества и практика (SQLite в data/, не в git).

case: вопрос сотрудника без ответа → письмо в СК. practice: итоговый ответ эксперта, опубликованный
в базу знаний (kind=practice), с областью действия (подразделения) и привязкой к пунктам.
"""
import json
import sqlite3
import time
import uuid

SCHEMA = """
create table if not exists cases(
  case_id text primary key, created real, employee text, question text, departments text,
  dialogue text, fragments text, reason text, message_id text, status text, updated real);
create table if not exists mails(
  mail_id text primary key, case_id text, direction text, sender text, body text, received real,
  kind text, processed integer default 0);
create table if not exists practices(
  practice_id text primary key, case_id text, created real, expert text, question text, answer text,
  applies_to text, clauses text, outcome text, status text, revoked_by text, revoked_at real);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def new_case(self, employee, question, departments, dialogue, fragments, reason):
        cid = "C" + uuid.uuid4().hex[:8].upper()
        now = time.time()
        self.db.execute("insert into cases values(?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, now, employee, question, json.dumps(departments, ensure_ascii=False),
                         json.dumps(dialogue, ensure_ascii=False), json.dumps(fragments, ensure_ascii=False),
                         reason, f"<{cid.lower()}@assistant.local>", "sent", now))
        self.db.commit()
        return self.case(cid)

    def case(self, cid):
        r = self.db.execute("select * from cases where case_id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def cases(self):
        return [dict(r) for r in self.db.execute("select * from cases order by created desc")]

    def set_status(self, cid, status):
        self.db.execute("update cases set status=?, updated=? where case_id=?", (status, time.time(), cid))
        self.db.commit()

    def add_mail(self, mail_id, cid, direction, sender, body, kind):
        self.db.execute("insert or ignore into mails values(?,?,?,?,?,?,?,0)",
                        (mail_id, cid, direction, sender, body, time.time(), kind))
        self.db.commit()

    def seen_mail(self, mail_id):
        return self.db.execute("select 1 from mails where mail_id=?", (mail_id,)).fetchone() is not None

    def publish(self, cid, expert, question, answer, applies_to, clauses, outcome, status="published"):
        pid = "P" + uuid.uuid4().hex[:8].upper()
        self.db.execute("insert into practices values(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, cid, time.time(), expert, question, answer, json.dumps(applies_to),
                         json.dumps(clauses, ensure_ascii=False), outcome, status, None, None))
        self.db.commit()
        return pid

    def approve(self, pid, who, question=None, answer=None, applies_to=None):
        row = self.db.execute("select * from practices where practice_id=?", (pid,)).fetchone()
        if not row:
            raise KeyError(pid)
        self.db.execute("update practices set status='published', question=?, answer=?, applies_to=?, revoked_by=? where practice_id=?",
                        (question or row["question"], answer or row["answer"],
                         json.dumps(applies_to) if applies_to else row["applies_to"], f"подтвердил: {who}", pid))
        self.db.execute("update cases set status='published', updated=? where case_id=?", (time.time(), row["case_id"]))
        self.db.commit()

    def revoke(self, pid, who):
        self.db.execute("update practices set status='revoked', revoked_by=?, revoked_at=? where practice_id=?",
                        (who, time.time(), pid))
        self.db.commit()

    def practices(self, only_published=True):
        q = "select * from practices" + (" where status='published'" if only_published else "") + " order by created"
        out = []
        for r in self.db.execute(q):
            d = dict(r)
            d["applies_to"] = json.loads(d["applies_to"])
            d["clauses"] = json.loads(d["clauses"])
            out.append(d)
        return out
