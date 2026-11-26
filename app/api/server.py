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

from app.dialog.clarify import label
from app.escalation import mail
from app.escalation.process import practice_windows, process_replies
from app.escalation.store import Store
from app.pipeline import run_turn
from app.registry.build import run as build_registry
from app.search.bm25 import BM25

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


class MockLLM:
    """Без стенда: «отвечает» первым фрагментом дословно, чтобы прошли проверки цитат."""
    def chat_json(self, system, user, schema, max_tokens=0):
        props = schema.get("properties", {})
        if "in_scope" in props:
            return {"in_scope": True}, {}
        if "is_final" in props:
            final = "уточн" not in user.lower().split("письмо эксперта")[-1]
            ans = user.split("\n", 3)[-1].strip()[:500]
            return {"is_final": final, "answer": ans, "applies_to": ["ALL"] if "всех" in user.lower() else [], "outcome": "practice"}, {}
        import re
        m = re.search(r"\[w1\][^\n]*\n(.+?)(?:\n\n\[w2\]|\n\nПодразделение:)", user, re.S)
        txt = (m.group(1) if m else "").strip()
        txt = re.sub(r"^\d+(?:\.\d+)*\.\s*", "", txt)
        first = txt[:220].rsplit(" ", 1)[0] if len(txt) > 220 else txt
        if not first:
            return {"verdict": "no_answer", "summary": "", "steps": [], "caveat": "", "missing": "нет фрагментов"}, {}
        return {"verdict": "answer", "summary": "(демо-режим без модели) " + first[:200], "caveat": "", "missing": "",
                "steps": [{"text": first, "who": "", "deadline": "", "ref": "w1", "quote": first}]}, {"seconds": 0}


class App:
    def __init__(self, corpus, data, mock=False):
        data.mkdir(parents=True, exist_ok=True)
        self.registry, wins = build_registry(Path(corpus), data / "index")
        self.windows = [w.to_dict() for w in wins]
        self.store = Store(str(data / "app.sqlite"))
        self.sessions = {}
        self.mock = mock
        self.qa_address = os.environ.get("QA_EMAIL", "quality@example.test")   # в демо — не реальный адрес СК
        self.smtp = (os.environ.get("MAIL_SMTP_HOST", "127.0.0.1"), int(os.environ.get("MAIL_SMTP_PORT", "1025")))
        self.mailpit = mail.Mailpit(os.environ.get("MAILPIT_API", "http://127.0.0.1:8025"))
        self.log = data / "journal.jsonl"
        if mock:
            self.llm = MockLLM()
            self._rebuild_mock()
        else:
            from app.answer.llm import LLM
            from app.search.hybrid import Hybrid
            from app.search.tei import TEI
            self.tei = TEI(os.environ["TEI_EMBED_URL"], os.environ["TEI_RERANK_URL"])
            self.llm = LLM(os.environ["LLM_BASE_URL"], os.environ.get("LLM_MODEL", "qwen38"))
            self.base_vecs = self.tei.embed([w["path"] + "\n" + w["text"] for w in self.windows])
            self.Hybrid = Hybrid
            self._rebuild_real()

    # --- индекс (регламенты + опубликованная практика) ---
    def all_windows(self):
        return self.windows + practice_windows(self.store)

    def _rebuild_mock(self):
        self.bm = BM25(self.all_windows())

    def _rebuild_real(self):
        pw = practice_windows(self.store)
        pv = self.tei.embed([w["path"] + "\n" + w["text"] for w in pw]) if pw else []
        self.hy = self.Hybrid(self.windows + pw, self.base_vecs + pv, self.tei.embed, self.tei.rerank)

    def rebuild(self):
        self._rebuild_mock() if self.mock else self._rebuild_real()

    def search(self, q, allowed):
        """Практика ищется вместе с документом подразделения: allowed расширяется practice:<dep>."""
        if allowed:
            deps = {d["department"] for d in self.registry if d["doc_id"] in allowed}
            allowed = set(allowed) | {f"practice:{d}" for d in deps}
        if self.mock:
            hits = self.bm.search(q, k=10, allowed=allowed)
            top = hits[0]["score"] if hits else 1
            return [{**h, "score": round(min(0.99, h["score"] / top), 3)} for h in hits]
        return self.hy.search(q, k=10, allowed=allowed)[0]

    # --- ход диалога ---
    def ask(self, sid, question, choice=None, employee=""):
        s = self.sessions.setdefault(sid, {"docs": [], "clarifications": 0, "history": [], "pending": None})
        q = question
        if choice is not None and s["pending"]:
            q = s["pending"]
            opts = s["pending_options"]
            if choice == "?":
                s["docs"] = [o["doc_id"] for o in opts if o["doc_id"] not in ("*", "?")]
                s["compare"] = True
            elif choice == "*":
                s["docs"] = [o["doc_id"] for o in opts if o["doc_id"] not in ("*", "?")]
            else:
                s["docs"] = choice.split(",")
            s["clarifications"] += 1
        t0 = time.time()
        turn = run_turn(q, self.registry, self.search, self.llm, session={"docs": s["docs"], "clarifications": s["clarifications"]})
        out = {"outcome": turn.outcome, "text": turn.text, "trace": turn.trace, "route": turn.route,
               "documents": turn.documents, "external_refs": turn.external_refs,
               "options": [{"id": o["doc_id"], "label": o["label"]} for o in turn.options], "answers": []}
        for a in turn.answers:
            r = a["result"]
            out["answers"].append({"label": a["label"], "verdict": r["verdict"], "summary": r["summary"], "caveat": r.get("caveat", ""),
                                   "missing": r.get("missing", ""), "steps": [
                                       {k: st.get(k, "") for k in ("text", "who", "deadline", "quote", "clause", "doc_title", "department")}
                                       | {"practice": st.get("doc_title", "").startswith("Практика")} for st in r["steps"]]})
        if turn.outcome == "clarify":
            s["pending"], s["pending_options"] = q, turn.options
        else:
            s["pending"] = None
        if turn.outcome == "escalate":
            frags = [{"doc": h.get("path", "").split(" › ")[0], "clause": h["clause"], "text": h["text"]} for h in turn.hits[:5]]
            deps = sorted({label(d, self.registry) for d in self.registry if d["doc_id"] in (s["docs"] or [])})
            case = self.store.new_case(employee, q, deps, s["history"][-6:], frags, "в регламентах нет опоры для ответа")
            try:
                mail.send(mail.case_email(case, self.qa_address), *self.smtp)
                out["case"] = {"id": case["case_id"], "to": self.qa_address, "cc": employee, "sent": True}
            except OSError as e:
                out["case"] = {"id": case["case_id"], "sent": False, "error": str(e)[:120]}
        out["seconds"] = round(time.time() - t0, 2)
        s["history"].append({"role": "user", "text": q})
        s["history"].append({"role": "assistant", "text": turn.outcome})
        with open(self.log, "a") as fh:
            fh.write(json.dumps({"t": time.time(), "sid": sid, "q": q, "outcome": turn.outcome, "trace": turn.trace,
                                 "seconds": out["seconds"]}, ensure_ascii=False) + "\n")
        return out

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
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

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
                return self._json(200, [{k: d[k] for k in ("display_title", "department_name", "stage", "windows", "status")} for d in app.registry])
            if self.path == "/api/cases":
                return self._json(200, app.store.cases())
            if self.path == "/api/practices":
                return self._json(200, app.store.practices(only_published=False))
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            try:
                b = self._body()
            except ValueError:
                return self._json(400, {"error": "тело запроса — не JSON"})
            try:
                if self.path == "/api/ask":
                    sid = b.get("session") or uuid.uuid4().hex
                    out = app.ask(sid, b.get("question", ""), b.get("choice"), b.get("employee", ""))
                    out["session"] = sid
                    return self._json(200, out)
                if self.path == "/api/reset":
                    app.sessions.pop(b.get("session"), None)
                    return self._json(200, {"ok": True})
                if self.path == "/api/qa/reply":          # имитация почтового клиента эксперта СК
                    case = app.store.case(b["case_id"])
                    mail.reply(case, b.get("from", app.qa_address), b["text"], *app.smtp)
                    return self._json(200, {"ok": True})
                if self.path == "/api/qa/poll":
                    return self._json(200, {"events": app.poll_mail()})
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
    ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
