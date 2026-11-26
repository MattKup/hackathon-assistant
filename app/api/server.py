"""HTTP-сервер ассистента (stdlib): чат, сессии, обращения в СК, почта СК (имитация), практика.

    python3 -m app.api.server --corpus <папка> --data data/app [--mock]

--mock: BM25 + заглушка LLM (для разработки интерфейса без стенда).
Иначе адреса из окружения: TEI_EMBED_URL, TEI_RERANK_URL, LLM_BASE_URL, MAIL_SMTP_HOST/PORT, MAILPIT_API.
"""
import argparse
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.answer.cache import AnswerCache
from app.dialog.clarify import label
from app.escalation import draft as qa_draft
from app.escalation import mail
from app.escalation.process import practice_windows, process_replies
from app.escalation.store import Store
from app.ingest.upload import Uploads, parse_multipart
from app.pipeline import load_conflicts, run_turn
from app.registry.build import run as build_registry
from app.search.bm25 import BM25
from app.search.vectors import VectorStore

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


class MockLLM:
    """Без стенда: «отвечает» первым фрагментом дословно, чтобы прошли проверки цитат."""
    def chat_json(self, system, user, schema, max_tokens=0):
        props = schema.get("properties", {})
        if "in_scope" in props:
            return {"in_scope": True}, {}
        if "queries" in props:
            return {"queries": [user.rsplit("Вопрос сотрудника:", 1)[-1].strip()], "departments": [], "stage": ""}, {}
        if "proposal" in props:
            return {"found": "(демо-режим) найденные пункты", "gap": "случай не оговорён", "proposal": "требует решения эксперта",
                    "clauses": []}, {}
        if "is_final" in props:
            mail_part = user.lower().split("письмо эксперта")[-1]
            final = "уточн" not in mail_part
            ans = user.split("\n", 3)[-1].strip()[:500]
            q = user.split("\n", 1)[0].replace("Вопрос сотрудника:", "").strip()
            return {"is_final": final, "answer": ans, "question_general": q, "variants": [q],
                    "applies_to": ["ALL"] if "всех" in mail_part else [], "outcome": "practice"}, {}
        import re
        m = re.search(r"\[w1\][^\n]*\n(.+?)(?:\n\n\[w2\]|\n\nПодразделение:)", user, re.S)
        txt = (m.group(1) if m else "").strip()
        txt = re.sub(r"^\d+(?:\.\d+)*\.\s*", "", txt)
        first = txt[:220].rsplit(" ", 1)[0] if len(txt) > 220 else txt
        if not first:
            return {"verdict": "no_answer", "summary": "", "steps": [], "caveat": "", "missing": "нет фрагментов",
                    "case_covered": True, "ask": "", "ask_options": []}, {}
        return {"verdict": "answer", "summary": "(демо-режим без модели) " + first[:200], "caveat": "", "missing": "",
                "case_covered": True, "ask": "", "ask_options": [],
                "steps": [{"text": first, "who": "", "deadline": "", "ref": "w1", "quote": first}]}, {"seconds": 0}


class App:
    def __init__(self, corpus, data, mock=False):
        data.mkdir(parents=True, exist_ok=True)
        self.registry, wins = build_registry(Path(corpus), data / "index")
        self.windows = [w.to_dict() for w in wins]
        self.store = Store(str(data / "app.sqlite"))
        self.lock = threading.Lock()
        self.answer_cache = AnswerCache()
        self.uploads = Uploads(str(data / "uploads.sqlite"), data / "uploads", os.environ.get("DOC_INTEL_URL", ""),
                               os.environ.get("DOC_INTEL_TOKEN_FILE", ""))
        for doc, ws in self.uploads.load_ready():
            self.registry.append(doc)
            self.windows += ws
        self.sessions = {}
        self.mock = mock
        self.qa_address = os.environ.get("QA_EMAIL", "quality@example.test")   # в демо — не реальный адрес СК
        self.smtp = (os.environ.get("MAIL_SMTP_HOST", "127.0.0.1"), int(os.environ.get("MAIL_SMTP_PORT", "1025")))
        self.mailpit = mail.Mailpit(os.environ.get("MAILPIT_API", "http://127.0.0.1:8025"))
        self.log = data / "journal.jsonl"
        if mock:
            self.llm = self.review_llm = MockLLM()
            self._pc0 = None
            self.review_model = "mock"
            self._rebuild_mock()
        else:
            from app.answer.llm import LLM
            from app.search.hybrid import Hybrid
            from app.search.tei import TEI
            self.tei = TEI(os.environ["TEI_EMBED_URL"], os.environ["TEI_RERANK_URL"])
            self.llm = LLM(os.environ["LLM_BASE_URL"], os.environ.get("LLM_MODEL", "qwen38"))
            # второй проход по обращениям (черновик для СК): можно направить на более крупную open-weights модель
            self.review_model = os.environ.get("LLM_REVIEW_MODEL") or os.environ.get("LLM_MODEL", "qwen38")
            self.review_llm = LLM(os.environ.get("LLM_REVIEW_URL") or os.environ["LLM_BASE_URL"], self.review_model)
            self.vec_store = VectorStore(data / "vectors.jsonl")       # векторы на диске: перезапуск без пересчёта
            self.vec = self.vec_store.load()
            self._pc0 = self.llm.prefix_cache()
            self.Hybrid = Hybrid
            self._rebuild_real()

    # --- индекс (регламенты + опубликованная практика) ---
    def all_windows(self):
        return self.windows + practice_windows(self.store)

    def _rebuild_mock(self):
        self.bm = BM25(self.all_windows())

    def _rebuild_real(self):
        allw = self.all_windows()
        missing = [w for w in allw if self.vec_store.key(w) not in self.vec]
        for i in range(0, len(missing), 256):                         # пачками: большая база не упирается в лимит TEI
            part = missing[i:i + 256]
            vs = self.tei.embed([w["path"] + "\n" + w["text"] for w in part])
            self.vec_store.add({self.vec_store.key(w): v for w, v in zip(part, vs)})
            self.vec.update({self.vec_store.key(w): v for w, v in zip(part, vs)})
        self.hy = self.Hybrid(allw, [self.vec[self.vec_store.key(w)] for w in allw], self.tei.embed, self.tei.rerank)

    def rebuild(self):
        with self.lock:
            self._rebuild_mock() if self.mock else self._rebuild_real()
            self.answer_cache.invalidate()        # индекс изменился — сохранённые ответы могут устареть
            self.conflicts = None                 # перечитать реестр противоречий с новым текстом пунктов

    # --- загрузка документов (служба качества) ---
    def upload(self, file_name, data, department, stage):
        import re as _re
        department = _re.sub(r"[^\w]", "", (department or "").upper())[:24] or "OTHER"   # новое подразделение — любым кодом
        stage = stage if stage in ("presale", "project", "full_cycle") else "full_cycle"
        doc_id, new = self.uploads.accept(file_name, data, department, stage)
        if new:
            threading.Thread(target=self._process_upload, args=(doc_id,), daemon=True).start()
        return doc_id, new

    def _process_upload(self, doc_id):
        try:
            doc, ws = self.uploads.process(doc_id)
        except Exception:
            return
        with self.lock:
            self.registry = [d for d in self.registry if d["doc_id"] != doc_id] + [doc]
            self.windows = [w for w in self.windows if w["doc_id"] != doc_id] + ws
        self.rebuild()

    def remove_upload(self, doc_id):
        with self.lock:
            self.registry = [d for d in self.registry if d["doc_id"] != doc_id]
            self.windows = [w for w in self.windows if w["doc_id"] != doc_id]
        self.uploads.remove(doc_id)
        self.rebuild()

    def cache_stats(self):
        pc = None if self.mock else self.llm.prefix_cache()
        if pc and self._pc0:
            dq, dh = pc["queries"] - self._pc0["queries"], pc["hits"] - self._pc0["hits"]
            pc = {**pc, "hit_rate_since_start": round(dh / dq, 3) if dq else None}
        return {"answers": self.answer_cache.stats(), "vllm_prefix": pc}

    def analytics(self):
        rows = []
        if self.log.exists():
            rows = [r for r in (json.loads(l) for l in self.log.read_text().splitlines() if l.strip()) if "outcome" in r]
        from collections import Counter
        out = Counter(r["outcome"] for r in rows)
        times = sorted(r.get("seconds", 0) for r in rows if r["outcome"] != "clarify")
        noans = Counter(r["q"] for r in rows if r["outcome"] in ("escalate", "answer_with_caveat") and r["q"])
        return {"questions": len(rows), "by_outcome": dict(out),
                "p50": times[len(times) // 2] if times else None, "p95": times[max(0, int(len(times) * .95) - 1)] if times else None,
                "no_answer_top": noans.most_common(10), "cases": len(self.store.cases()),
                "practices": sum(1 for p in self.store.practices(only_published=False) if p["status"] == "published"),
                "recent": rows[-15:][::-1], "cache": self.cache_stats()}

    def search(self, q, allowed, k=16):
        """Практика ищется вместе с документом подразделения: allowed расширяется practice:<dep>."""
        if allowed:
            deps = {d["department"] for d in self.registry if d["doc_id"] in allowed}
            allowed = set(allowed) | {f"practice:{d}" for d in deps}
        if self.mock:
            hits = self.bm.search(q, k=k, allowed=allowed)
            top = hits[0]["score"] if hits else 1
            return [{**h, "score": round(min(0.99, h["score"] / top), 3)} for h in hits]
        return self.hy.search(q, k=k, allowed=allowed)[0]

    # --- ход диалога ---
    def ask(self, sid, question, choice=None, employee="", no_clarify=None):
        s = self.sessions.setdefault(sid, {"docs": [], "clarifications": 0, "history": [], "pending": None})
        if no_clarify is not None:
            s["no_clarify"] = bool(no_clarify)      # режим «без уточнения дирекции»: сразу ответы по всем дирекциям
        q = question
        if choice is not None and s["pending"] and s.get("pending_kind") == "ask":
            q = s["pending"]                         # ответ на уточняющий вопрос (условие или тема)
            s["ask_reply"] = next((o["label"] for o in s["pending_options"] if o["doc_id"] == choice), choice)
        elif choice is not None and s["pending"]:
            q = s["pending"]
            opts = s["pending_options"]
            depts = [o["doc_id"] for o in opts if o["doc_id"] not in ("*", "?")]
            chosen = depts if choice in ("?", "*") else [c for c in choice.split(",") if c]
            s["docs"] = [d["doc_id"] for d in self.registry if d["department"] in chosen]
            s["clarifications"] += 1
            s["complex"] = choice == "*" or "," in choice
        elif q:
            s["complex"] = False
            s["clarifications"] = 0                  # бюджет уточнений — на вопрос, а не на весь чат (Fable К1)
            s.pop("cache", None)
            s.pop("ask", None)
        t0 = time.time()
        if getattr(self, "conflicts", None) is None:
            self.conflicts = load_conflicts(self.windows)          # реестр подтверждённых противоречий
        turn = run_turn(q, self.registry, self.search, self.llm, session=s, answer_cache=self.answer_cache,
                        conflicts=self.conflicts)   # s хранит кэш ответов по дирекциям для второго хода
        out = {"outcome": turn.outcome, "text": turn.text, "tokens": turn.tokens, "trace": turn.trace, "route": turn.route,
               "documents": turn.documents, "external_refs": turn.external_refs,
               "options": [{"id": o["doc_id"], "label": o["label"]} for o in turn.options], "answers": []}
        for a in turn.answers:
            r = a["result"]
            out["answers"].append({"label": a["label"], "verdict": r["verdict"], "summary": r["summary"], "caveat": r.get("caveat", ""),
                                   "missing": r.get("missing", ""), "steps": [
                                       {k: st.get(k, "") for k in ("text", "who", "deadline", "quote", "clause", "doc_title", "department", "source_text", "section")}
                                       | {"practice": st.get("doc_title", "").startswith("Практика")} for st in r["steps"]]})
        if turn.outcome in ("clarify", "ask"):
            s["pending"], s["pending_options"], s["pending_kind"] = turn.question or q, turn.options, turn.outcome
        else:
            s["pending"] = None
        s["last"] = {"q": q, "hits": turn.hits[:5], "employee": employee,
                     "depts": [a["label"] for a in turn.answers if a["result"]["verdict"] != "no_answer"]}
        if turn.outcome == "escalate" or (turn.route or {}).get("email"):
            why = (turn.route or {}).get("why") or "в регламентах нет опоры для ответа"
            out["case"] = self.escalate(sid, why)
        out["seconds"] = round(time.time() - t0, 2)
        s["history"].append({"role": "user", "text": q})
        s["history"].append({"role": "assistant", "text": turn.outcome})
        with open(self.log, "a") as fh:
            fh.write(json.dumps({"t": time.time(), "sid": sid, "q": q, "outcome": turn.outcome, "trace": turn.trace,
                                 "seconds": out["seconds"]}, ensure_ascii=False) + "\n")
        return out

    def escalate(self, sid, reason):
        s = self.sessions.get(sid) or {}
        last = s.get("last") or {}
        frags = [{"doc": h.get("path", "").split(" › ")[0], "clause": h["clause"], "text": h["text"]} for h in last.get("hits", [])]
        deps = last.get("depts") or sorted({label(d, self.registry) for d in self.registry if d["doc_id"] in (s.get("docs") or [])})
        dup = next((c for c in self.store.cases() if c["question"] == last.get("q", "") and c["employee"] == last.get("employee", "")
                    and time.time() - c["created"] < 2 * 3600 and c["status"] in ("sent", "interim")), None)
        if dup:                                   # тот же вопрос того же сотрудника — новое письмо не шлём (ревью Fable К14)
            return {"id": dup["case_id"], "to": self.qa_address, "cc": last.get("employee", ""), "sent": "already"}
        case = self.store.new_case(last.get("employee", ""), last.get("q", ""), deps, s.get("history", [])[-6:], frags, reason)

        def work():
            """Черновик для эксперта (второй проход LLM) → письмо в СК. В фоне: сотрудник не ждёт."""
            d = None
            try:
                d = qa_draft.prepare(self.review_llm, case, self.review_model)
            except Exception as e:  # без черновика письмо всё равно уходит
                d = {"error": str(e)[:120]}
            try:
                mail.send(mail.case_email(case, self.qa_address, d if d and "error" not in d else None), *self.smtp)
                self.store.save_draft(case["case_id"], d, True)
            except OSError:
                self.store.save_draft(case["case_id"], d, False)
        threading.Thread(target=work, daemon=True).start()
        return {"id": case["case_id"], "to": self.qa_address, "cc": last.get("employee", ""), "sent": "queued"}

    def poll_mail(self):
        ev = process_replies(self.store, self.mailpit, self.llm)
        if any(e["event"] == "published" for e in ev):
            self.rebuild()
        return ev


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def _json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                raw = b""
                while True:
                    size = int(self.rfile.readline().strip() or b"0", 16)
                    if not size:
                        self.rfile.readline()
                        break
                    raw += self.rfile.read(size)
                    self.rfile.readline()
            else:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._raw = raw
            return json.loads(raw or b"{}")

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = (UI / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if self.path == "/api/health":
                return self._json(200, {"ok": True, "mock": app.mock, "windows": len(app.windows)})
            if self.path == "/api/documents":
                ups = {r["doc_id"]: r for r in app.uploads.rows()}
                docs = [{"doc_id": d["doc_id"], "display_title": d["display_title"], "department": d["department"],
                         "department_name": label(d, app.registry), "stage": d["stage"], "windows": d.get("windows"),
                         "status": "действует", "source": "загружен" if d.get("uploaded") else "корпус хакатона",
                         "parser": d.get("parser"), "pages": (ups.get(d["doc_id"]) or {}).get("pages"),
                         "ocr_pages": (ups.get(d["doc_id"]) or {}).get("ocr_pages")} for d in app.registry]
                pending = [{"doc_id": r["doc_id"], "display_title": r["file_name"], "department": r["department"], "stage": r["stage"],
                            "status": {"processing": "распознаётся…", "error": "ошибка: " + (r["error"] or "")}.get(r["status"], r["status"]),
                            "source": "загружен", "parser": r["parser"]} for r in ups.values() if r["status"] != "ready"]
                return self._json(200, pending + docs)
            if self.path == "/api/analytics":
                return self._json(200, app.analytics())
            if self.path == "/api/quality":
                f = UI / "quality.json"
                return self._json(200, json.loads(f.read_text()) if f.exists() else {})
            if self.path == "/api/cases":
                return self._json(200, app.store.cases())
            if self.path == "/api/practices":
                return self._json(200, app.store.practices(only_published=False))
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path == "/api/docs/upload":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 55 * 1024 * 1024:
                        return self._json(413, {"error": "файл больше 50 МБ"})
                    fields, files = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(n))
                    if "file" not in files:
                        return self._json(400, {"error": "нет файла"})
                    doc_id, new = app.upload(files["file"][0], files["file"][1], fields.get("department", "TECH"), fields.get("stage", "full_cycle"))
                    return self._json(200, {"doc_id": doc_id, "new": new})
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
            try:
                b = self._body()
            except ValueError:
                return self._json(400, {"error": "тело запроса — не JSON", "head": repr(getattr(self, "_raw", b"")[:120]),
                                        "headers": {k: v for k, v in self.headers.items() if k.lower() in ("content-length", "transfer-encoding", "content-type")}})
            try:
                if self.path == "/api/ask":
                    sid = b.get("session") or uuid.uuid4().hex
                    out = app.ask(sid, b.get("question", ""), b.get("choice"), b.get("employee", ""), b.get("no_clarify"))
                    out["session"] = sid
                    return self._json(200, out)
                if self.path == "/api/admin/reset-demo":   # очистить обращения, карточки, журнал (демо)
                    app.store.db.executescript("delete from cases; delete from mails; delete from practices;")
                    app.store.db.commit()
                    app.log.write_text("")
                    app.sessions.clear()
                    app.rebuild()
                    return self._json(200, {"ok": True})
                if self.path == "/api/feedback":
                    with open(app.log, "a") as fh:
                        fh.write(json.dumps({"t": time.time(), "sid": b.get("session"), "feedback": b.get("value")}) + "\n")
                    return self._json(200, {"ok": True})
                if self.path == "/api/docs/remove":
                    app.remove_upload(b["doc_id"])
                    return self._json(200, {"ok": True})
                if self.path == "/api/escalate":          # «Уточнить у службы качества» под ответом с оговоркой
                    return self._json(200, {"case": app.escalate(b["session"], b.get("reason") or "сотрудник просит уточнить ответ с оговоркой")})
                if self.path == "/api/reset":
                    app.sessions.pop(b.get("session"), None)
                    return self._json(200, {"ok": True})
                if self.path == "/api/qa/reply":          # имитация почтового клиента эксперта СК
                    case = app.store.case(b["case_id"])
                    mail.reply(case, b.get("from", app.qa_address), b["text"], *app.smtp)
                    return self._json(200, {"ok": True})
                if self.path == "/api/qa/poll":
                    return self._json(200, {"events": app.poll_mail()})
                if self.path == "/api/qa/approve":        # эксперт подтверждает/правит карточку знания
                    app.store.approve(b["practice_id"], b.get("who", "служба качества"), b.get("question"), b.get("answer"), b.get("applies_to"))
                    app.rebuild()
                    return self._json(200, {"ok": True})
                if self.path == "/api/qa/revoke":
                    app.store.revoke(b["practice_id"], b.get("who", "qa"))
                    app.rebuild()
                    return self._json(200, {"ok": True})
            except Exception as e:  # ответ клиенту, без падения сервера
                return self._json(500, {"error": str(e)[:300]})
            return self._json(404, {"error": "not found"})
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--data", default="data/app")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--host", default="127.0.0.1", help="адрес прослушивания (наружу — только через Caddy)")
    ap.add_argument("--poll", type=int, default=15, help="опрос почты СК, секунд (0 — выкл.)")
    a = ap.parse_args()
    app = App(a.corpus, Path(a.data), a.mock)
    if a.poll:
        def loop():
            while True:
                time.sleep(a.poll)
                try:
                    app.poll_mail()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()
    print(f"assistant on :{a.port} mock={a.mock} windows={len(app.windows)}", flush=True)
    ThreadingHTTPServer((a.host, a.port), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
