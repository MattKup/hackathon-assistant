"""HTTP-сервер ассистента (stdlib): чат, сессии, обращения в СК, почта СК (имитация), практика.

    python3 -m app.api.server --corpus <папка> --data data/app [--mock]

--mock: BM25 + заглушка LLM (для разработки интерфейса без стенда).
Иначе адреса из окружения: TEI_EMBED_URL, TEI_RERANK_URL, LLM_BASE_URL, MAIL_SMTP_HOST/PORT, MAILPIT_API.
"""
import argparse
import dataclasses
import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.analysis.registry import ConflictRegistry
from app.answer.cache import AnswerCache
from app.dialog.clarify import dept_followup, label
from app.answer.aspects import EXTERNAL, EXTERNAL_NAME, SHOW_FOR, external_hits, is_external, match_rules, parse_rules, rules_to_lines
from app.escalation import draft as qa_draft
from app.escalation import mail
from app.escalation import repeats
from app.escalation.repeats import Repeats
from app.escalation.process import practice_windows, process_replies
from app.escalation.store import Store
from app.ingest.upload import Uploads, parse_multipart
from app.pipeline import OFF, build_index, doc_abbr, load_conflicts, run_turn
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
        self.data = data
        self.registry, wins = build_registry(Path(corpus), data / "index")
        self.windows = [w.to_dict() for w in wins]
        self.store = Store(str(data / "app.sqlite"))
        self.conflict_reg = ConflictRegistry(str(data / "app.sqlite"))   # кандидаты анализатора и решения СК
        manual = ROOT / "config" / "conflicts_candidates_manual.json"   # найденные человеком — тоже только кандидаты
        if manual.exists():
            m = json.loads(manual.read_text())
            self.conflict_reg.import_manual(m.get("pairs", []), self.windows, m.get("source", "вручную"))
        self.lock = threading.Lock()
        self.answer_cache = AnswerCache()
        self.uploads = Uploads(str(data / "uploads.sqlite"), data / "uploads", os.environ.get("DOC_INTEL_URL", ""),
                               os.environ.get("DOC_INTEL_TOKEN_FILE", ""))
        for doc, ws in self.uploads.load_ready():
            self.registry.append(doc)
            self.windows += ws
        self.sessions = {}
        self.live = {}                 # идущие ходы: rid → шаги работы (для показа ожидания в вебе)
        self.live_kind = {}            # rid → сессия хода (только чтобы отдать текущий этап, _live_kind)
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
        from app import config as _config
        _config.use_overrides(data / "settings.json")   # настройки СК из веба (темы СК, мониторинг повторов) поверх конфигурации
        rc = repeats.settings()                  # мониторинг повторов: период, порог, похожесть — из конфигурации
        similar = repeats.words_similar(rc["similarity_words"]) if mock else repeats.vectors_similar(self.tei.embed, rc["similarity"])
        self.repeats = Repeats(self.store.db, similar, rc)

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
            self.conflicts = None                 # индекс пунктов, расшифровки и сокращения документов пересоберутся при следующем вопросе
            self.conflicts = None                 # перечитать реестр противоречий с новым текстом пунктов

    # --- загрузка документов (служба качества) ---
    def search_ext(self, q, allowed, k=2):
        """Поиск только по внешним источникам (минуя фильтр основного поиска)."""
        if self.mock:
            hits = self.bm.search(q, k=k, allowed=allowed)
            top = hits[0]["score"] if hits else 1
            return [{**h, "score": round(min(0.99, h["score"] / top), 3)} for h in hits]
        return self.hy.search(q, k=k, allowed=allowed)[0]

    def upload(self, file_name, data, department, stage, department_name="", doc_kind=""):
        import re as _re
        if doc_kind == "external":                   # внешний источник: только для «Обратите внимание», не регламент
            department, department_name, stage = EXTERNAL, EXTERNAL_NAME, "full_cycle"
        department_name = " ".join((department_name or "").split())[:80]
        if department_name and not department:      # новое подразделение: код — из названия, название — как ввели
            department = department_name
        department = _re.sub(r"[^\w]", "", (department or "").upper())[:24] or "OTHER"   # новое подразделение — любым кодом
        stage = stage if stage in ("presale", "project", "full_cycle") else "full_cycle"
        doc_id, new = self.uploads.accept(file_name, data, department, stage, department_name)
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

    def confluence_sync(self, space=None):
        """Синхронизация пространств Confluence (config/documents.json «confluence_spaces»: ключ → подразделение, стадия).
        Адрес и учётная запись — переменные окружения CONFLUENCE_URL, CONFLUENCE_EMAIL (для Cloud), токен — файл
        CONFLUENCE_TOKEN_FILE; токен в лог и ответ не попадает."""
        from app.config import cfg
        from app.connectors.confluence import Confluence, sync
        url, tf = os.environ.get("CONFLUENCE_URL", ""), os.environ.get("CONFLUENCE_TOKEN_FILE", "")
        if not url or not tf or not Path(tf).exists():
            return {"error": "Confluence не настроен: CONFLUENCE_URL и CONFLUENCE_TOKEN_FILE"}
        conn = Confluence(url, Path(tf).read_text().strip(), os.environ.get("CONFLUENCE_EMAIL", ""))
        spaces = cfg().get("confluence_spaces") or {}
        out = {}
        for key, m in spaces.items():
            if space and key != space:
                continue
            out[key] = sync(conn, key, m.get("department", "OTHER"), m.get("stage", "full_cycle"),
                            lambda n, d, dep, st: self.upload(n, d, dep, st)[0], self.remove_upload,
                            str(self.data / "confluence.sqlite"))
        return out

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
                "recent": rows[-15:][::-1], "cache": self.cache_stats(),
                "repeats": {"groups": self.repeats.groups()[:10], **{k: self.repeats.conf[k] for k in ("period_days", "threshold")}}}

    def search(self, q, allowed, k=16):
        """Практика ищется вместе с документом подразделения: allowed расширяется practice:<dep>.
        Внешние источники («Обратите внимание») в основной поиск не попадают."""
        ext = {d["doc_id"] for d in self.registry if is_external(d)}
        if ext and not allowed:
            allowed = {d["doc_id"] for d in self.registry if not is_external(d)}
        if allowed:
            deps = {d["department"] for d in self.registry if d["doc_id"] in allowed}
            allowed = set(allowed) | {f"practice:{d}" for d in deps}
        if self.mock:
            hits = self.bm.search(q, k=k, allowed=allowed)
            top = hits[0]["score"] if hits else 1
            return [{**h, "score": round(min(0.99, h["score"] / top), 3)} for h in hits]
        return self.hy.search(q, k=k, allowed=allowed)[0]

    # --- быстрый ответ (27.11): готовый ход на точный повтор вопроса, пока база не менялась ---
    FAST_OUTCOMES = ("answer", "answer_with_caveat", "clarify", "document")
    FAST_NOTE = "Такой вопрос уже задавали, регламенты с тех пор не менялись — показываю проверенный ответ без повторного поиска"

    def _turn_key(self, q, s):
        """Ключ готового хода: точный текст вопроса (с точностью до пробелов), режимы диалога, состав базы и настройки СК.
        Версия индекса — внутри answer_cache.key: загрузка документа, карточка СК, решение по противоречию, сброс демо
        сбрасывают и готовые ходы."""
        try:
            stamp = (self.data / "settings.json").stat().st_mtime_ns
        except OSError:
            stamp = 0
        return self.answer_cache.key("turn", re.sub(r"\s+", " ", q).strip(), bool(s.get("no_clarify")), bool(s.get("hints")),
                                     bool(s.get("no_deep")), stamp, tuple(sorted(d["doc_id"] for d in self.registry)))

    def _turn(self, q, s, fresh):
        """Ход диалога; для нового вопроса (не выбор и не продолжение) — сначала готовый ход. Сохраняются только ходы без
        побочных действий: ответ, ответ с оговоркой, уточнение дирекции, «где описано». Письма в СК, уточнение условия,
        отказы и сбои модели каждый раз считаются заново. Выключатель: HK_OFF=FAST."""
        key = self._turn_key(q, s) if fresh and "FAST" not in OFF else None
        hit = self.answer_cache.get(key) if key else None
        if hit:
            if hit["cache"]:
                s["cache"] = hit["cache"]            # ответы по дирекциям для хода после уточнения — как при обычном ходе
            t = hit["turn"]
            return dataclasses.replace(t, trace=[self.FAST_NOTE] + list(t.trace),
                                       tokens={"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "by_step": {}}), True
        if getattr(self, "conflicts", None) is None:
            self.conflicts = load_conflicts(self.windows, self.conflict_reg.confirmed())   # только подтверждённые СК
            self.index = build_index(self.windows)                  # пункты по номеру — для агентного прохода
        if getattr(self, "_abbr_n", None) != len(self.windows):   # сокращения документов — пересчёт при изменении базы
            self.abbr, self._abbr_n = doc_abbr(self.windows, self.registry), len(self.windows)
        key = self._turn_key(q, s) if key else None    # версия индекса на начало хода: пересборка во время хода — ход не сохраняем
        turn = run_turn(q, [d for d in self.registry if not is_external(d)], self.search, self.llm, session=s, answer_cache=self.answer_cache,
                        conflicts=self.conflicts, index=self.index, abbr=self.abbr)   # s хранит кэш ответов по дирекциям для второго хода
        if key and turn.outcome in self.FAST_OUTCOMES and not (turn.route or {}).get("email") and not s.get("ask") \
                and not any(a["result"].get("error") or (a.get("meta") or {}).get("error") for a in turn.answers) \
                and key == self._turn_key(q, s):
            c = s.get("cache") if turn.outcome == "clarify" else None
            if turn.outcome != "clarify" or (c and c.get("q") == q):
                self.answer_cache.put(key, {"turn": turn, "cache": c})
        return turn, False

    def warm(self, limit=30):
        """Прогрев при запуске: последние вопросы из журнала стенда прогоняются заранее (в фоне, по одному), чтобы на
        показе они отвечались сразу. Идёт тем же путём, что и обычный вопрос, но без диалога и без писем: ход с письмом
        в СК не сохраняется и письмо не отправляется (обращения создаёт только ask). Выключатель: HK_OFF=WARM."""
        qs = []
        try:
            for line in reversed(self.log.read_text().splitlines()):
                r = json.loads(line)
                q = (r.get("q") or "").strip()
                if q and r.get("outcome") in self.FAST_OUTCOMES and q not in qs:
                    qs.append(q)
                if len(qs) >= limit:
                    break
        except (OSError, ValueError):
            pass
        done = 0
        for q in qs:
            try:
                self._turn(q, {"docs": [], "clarifications": 0, "history": [], "pending": None}, True)
                done += 1
            except Exception:
                pass
        return done

    # --- ход диалога ---
    def ask(self, sid, question, choice=None, employee="", no_clarify=None, hints=None, rid=None):
        s = self.sessions.setdefault(sid, {"docs": [], "clarifications": 0, "history": [], "pending": None})
        fresh = False
        if no_clarify is not None:
            s["no_clarify"] = bool(no_clarify)      # режим «без уточнения дирекции»: сразу ответы по всем дирекциям
        if hints is not None:
            s["hints"] = bool(hints)                # Г15 (опция): «проверьте также п. X» — соседние пункты с исключением
        q = question
        if not (q or "").strip() and (choice is None or not s.get("pending")):
            # кнопка из старого диалога после перезапуска сервера или пустой вопрос — не создавать пустое обращение (Fable 27.11)
            return {"session": sid, "outcome": "stale", "text": "Этот диалог устарел — задайте вопрос заново.", "answers": [],
                    "options": [], "trace": [], "seconds": 0, "tokens": {}, "route": None, "documents": [], "external_refs": []}
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
        elif q and s.get("last_q") and (fu := dept_followup(q, self.registry)):
            q = s["last_q"]                          # «а по ДБРЦТ?» — прошлый вопрос для названной дирекции
            s["docs"] = fu
            s["complex"] = False
            s.pop("ask", None)
        elif q:
            s["docs"] = []                           # новый вопрос — заново по всем дирекциям (Fable 27.11: выбранная ранее
                                                     # дирекция «залипала», и новый вопрос искался только в ней)
            s["complex"] = False
            s["clarifications"] = 0                  # бюджет уточнений — на вопрос, а не на весь чат (Fable К1)
            fresh = True
            s.pop("ask", None)                       # кэш ответов по дирекциям не сбрасываем: он привязан к тексту вопроса,
                                                     # повтор вопроса в том же чате отвечается сразу (владелец 27.11: 19 с)
        t0 = time.time()
        if rid:
            self.live[rid] = s["_live"] = []         # ход работы для веба: /api/progress?rid=…
            self.live_kind[rid] = s
        try:
            turn, cached = self._turn(q, s, fresh)
        finally:
            s.pop("_live", None)
            self.live.pop(rid, None)
            self.live_kind.pop(rid, None)
            s.pop("_live_kind", None)
        s["last_q"] = turn.question or q             # для продолжений «а по <дирекция>?»
        aspects = []
        if turn.outcome in SHOW_FOR:                 # «Обратите внимание — не из регламента» (организаторы 27.11, вариант А)
            from app.config import cfg as _cfg
            aspects = match_rules(turn.question or q, _cfg().get("aspects") or [])
            ext = [d["doc_id"] for d in self.registry if is_external(d)]
            if ext:
                try:
                    aspects += external_hits(lambda qq, al, k: self.search_ext(qq, al, k), turn.question or q, ext)
                except Exception:
                    pass
        out = {"outcome": turn.outcome, "text": turn.text, "tokens": turn.tokens, "trace": turn.trace, "route": turn.route,
               "documents": turn.documents, "external_refs": turn.external_refs,
               "options": [{"id": o["doc_id"], "label": o["label"]} for o in turn.options], "answers": [], "aspects": aspects,
               "cached": cached}
        for a in turn.answers:
            r = a["result"]
            out["answers"].append({"label": a["label"], "verdict": r["verdict"], "summary": r["summary"], "caveat": r.get("caveat", ""),
                                   "missing": r.get("missing", ""), "steps": [
                                       {k: st.get(k, "") for k in ("text", "who", "deadline", "quote", "clause", "doc_title", "department", "source_text", "section", "label")}
                                       | {"practice": st.get("doc_title", "").startswith("Практика")} for st in r["steps"]],
                                   "related": r.get("related", []),
                                   "rejected": [{"reason": x.get("reason", ""), "text": (x.get("text") or "")[:300], "quote": (x.get("quote") or "")[:300],
                                                 "replaced": bool(x.get("replaced_by_quote"))} for x in r.get("rejected", [])],
                                   "conflict_with": [{"clause": c["clause"], "why": c.get("why", ""), "text": c.get("text", "")[:600]}
                                                     for c in r.get("conflict_with", [])],
                                   "deep": bool((a.get("meta") or {}).get("deep", {}).get("better")),
                                   "from_cache": bool((a.get("meta") or {}).get("from_cache"))})
        if turn.outcome in ("clarify", "ask"):
            s["pending"], s["pending_options"], s["pending_kind"] = turn.question or q, turn.options, turn.outcome
        else:
            s["pending"] = None
        s["last"] = {"q": q, "hits": turn.hits[:5], "employee": employee,
                     "depts": [a["label"] for a in turn.answers if a["result"]["verdict"] != "no_answer"]}
        for a in turn.answers:                           # расхождение карточки СК с пунктом — в отчёт СК, не в ответ сотруднику
            if a["result"].get("discrepancy"):
                pids = sorted({w.get("window_id") for w in a.get("windows") or [] if w.get("kind") == "practice"})
                self.store.add_discrepancy(q, a["label"], a["result"]["discrepancy"], pids)
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
        own_repeat = dup is not None              # повтор того же сотрудника не считаем — иначе один человек «накрутит» порог
        if dup is None and reason.startswith("противоречие") and ":" in reason:
            # по одной паре противоречия служба качества получает одно письмо, а не на каждый вопрос (ревью Fable)
            dup = next((c for c in self.store.cases() if c["reason"] == reason and time.time() - c["created"] < 7 * 86400
                        and c["status"] in ("sent", "interim")), None)
        if dup:                                   # тот же вопрос того же сотрудника — новое письмо не шлём (ревью Fable К14)
            if not own_repeat:
                self._count_repeat(last.get("q", ""), last.get("employee", ""), dup["case_id"], reason)
            return {"id": dup["case_id"], "to": self.qa_address, "cc": last.get("employee", ""), "sent": "already"}
        case = self.store.new_case(last.get("employee", ""), last.get("q", ""), deps, s.get("history", [])[-6:], frags, reason)
        self._count_repeat(case["question"], case["employee"], case["case_id"], reason)

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

    def _count_repeat(self, question, employee, case_id, reason):
        """«Этот вопрос передавали N раз за период» → письмо-сигнал СК (одно на вопрос за период). В фоне: эмбеддинги."""
        def work():
            try:
                r = self.repeats.record(question, employee, case_id, reason)
                if r["signal"]:
                    mail.send(repeats.signal_email(r["signal"], self.qa_address), *self.smtp)
                    self.repeats.mark_sent(r["signal"]["signal_id"])
            except Exception:
                pass                              # сигнал — дополнительный; сбой не мешает обращению
        threading.Thread(target=work, daemon=True).start()

    _poll_lock = threading.Lock()

    def poll_mail(self):
        # фоновый опрос почты и опрос после «Ответить» шли одновременно и разбирали одно письмо дважды → две карточки (28.11)
        with self._poll_lock:
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
            if self.path.startswith("/api/progress"):      # что помощник делает сейчас — веб опрашивает, пока ждёт ответ
                from urllib.parse import parse_qs, urlparse
                rid = (parse_qs(urlparse(self.path).query).get("rid") or [""])[0]
                return self._json(200, {"steps": list(app.live.get(rid) or []), "kind": (app.live_kind.get(rid) or {}).get("_live_kind", "")})
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
                            "department_name": r["department_name"] or r["department"],
                            "status": {"processing": "распознаётся…", "error": "ошибка: " + (r["error"] or "")}.get(r["status"], r["status"]),
                            "source": "загружен", "parser": r["parser"]} for r in ups.values() if r["status"] != "ready"]
                return self._json(200, pending + docs)
            if self.path.startswith("/api/doc?"):          # полный текст документа по пунктам — просмотр в «Документах»
                from urllib.parse import parse_qs, urlparse
                did = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
                d = next((x for x in app.registry if x["doc_id"] == did), None)
                if not d:
                    return self._json(404, {"error": "документ не найден"})
                ws = [w for w in app.windows if w["doc_id"] == did]
                return self._json(200, {"title": d.get("display_title"), "department_name": label(d, app.registry),
                                        "items": [{"clause": w.get("clause") or "", "kind": w.get("kind") or "", "text": w["text"],
                                                   "section": w.get("section_title") or "", "part": w.get("part")} for w in ws]})
            if self.path == "/api/analytics":
                return self._json(200, app.analytics())
            if self.path == "/api/quality":
                f = UI / "quality.json"
                return self._json(200, json.loads(f.read_text()) if f.exists() else {})
            if self.path.startswith("/api/conflicts"):
                from urllib.parse import parse_qs, urlparse
                st = (parse_qs(urlparse(self.path).query).get("status") or [None])[0]
                return self._json(200, app.conflict_reg.pairs(st if st in ("candidate", "confirmed", "rejected") else None, app.windows))
            if self.path == "/api/cases":
                return self._json(200, app.store.cases())
            if self.path == "/api/discrepancies":
                return self._json(200, app.store.discrepancies())
            if self.path == "/api/settings":
                from app import config as _config
                st = {k: _config.cfg().get(k) for k in _config.EDITABLE}
                st["aspects"] = rules_to_lines(st.get("aspects") or [])
                return self._json(200, st)
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
                    doc_id, new = app.upload(files["file"][0], files["file"][1], fields.get("department", ""), fields.get("stage", "full_cycle"),
                                             fields.get("department_name", ""), fields.get("doc_kind", ""))
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
                    out = app.ask(sid, b.get("question", ""), b.get("choice"), b.get("employee", ""), b.get("no_clarify"), b.get("hints"),
                                  str(b.get("rid") or "")[:64] or None)
                    out["session"] = sid
                    return self._json(200, out)
                if self.path == "/api/admin/reset-demo":   # очистить обращения, карточки, журнал (демо)
                    def _commit(db):                      # повторный сброс: транзакции может не быть (sqlite «no transaction is active»)
                        try:
                            db.commit()
                        except Exception:
                            pass
                    for sql in ("delete from cases", "delete from mails", "delete from practices", "delete from drafts",
                                "delete from discrepancies"):
                        app.store.db.execute(sql)
                    _commit(app.store.db)
                    # Fable 27.11: сброс демо должен возвращать и решения по противоречиям, и счётчик повторов
                    for sql in ("delete from escalation_log", "delete from repeat_signals",
                                "update conflict_pairs set status='candidate', decided_by=null, decided_at=null, comment=null"):
                        try:
                            app.conflict_reg.db.execute(sql) if "conflict" in sql else app.store.db.execute(sql)
                        except Exception:
                            pass
                    _commit(app.store.db); _commit(app.conflict_reg.db)
                    app.conflicts = None                  # перечитать подтверждённые пары при следующем вопросе
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
                if self.path == "/api/connectors/confluence/sync":
                    return self._json(200, app.confluence_sync(b.get("space")))
                if self.path == "/api/qa/poll":
                    return self._json(200, {"events": app.poll_mail()})
                if self.path == "/api/qa/approve":        # эксперт подтверждает/правит карточку знания
                    app.store.approve(b["practice_id"], b.get("who", "служба качества"), b.get("question"), b.get("answer"), b.get("applies_to"),
                                      b.get("destination"))
                    app.rebuild()
                    return self._json(200, {"ok": True})
                if self.path == "/api/settings":          # служба качества меняет темы СК и мониторинг повторов
                    from app import config as _config
                    ch = {}
                    if isinstance(b.get("qa_topics"), list):
                        ch["qa_topics"] = [str(t).strip()[:80] for t in b["qa_topics"] if str(t).strip()][:30]
                    if isinstance(b.get("aspects"), list):
                        ch["aspects"] = parse_rules(b["aspects"])
                    rm = b.get("repeat_monitor")
                    if isinstance(rm, dict):
                        try:
                            ch["repeat_monitor"] = {**repeats.settings(), "period_days": max(1, min(90, int(rm["period_days"]))),
                                                    "threshold": max(2, min(50, int(rm["threshold"])))}
                        except (KeyError, ValueError, TypeError):
                            return self._json(400, {"error": "repeat_monitor: period_days и threshold — целые числа"})
                    out = _config.save_overrides(ch)
                    app.repeats.conf = repeats.settings()
                    return self._json(200, out)
                if self.path == "/api/qa/destination":    # куда ответ СК: база ответов (faq) или предложение в регламент
                    try:
                        app.store.set_destination(b["practice_id"], b.get("destination"))
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                    return self._json(200, {"ok": True})
                if self.path == "/api/conflicts/decide":   # служба качества: «Подтвердить» / «Не противоречие» / «Вернуть»
                    if b.get("decision") not in ("confirm", "reject", "reopen"):
                        return self._json(400, {"error": "decision: confirm | reject | reopen"})
                    app.conflict_reg.decide(b["pair_id"], b["decision"], b.get("who") or "служба качества", b.get("comment", ""))
                    app.conflicts = None                  # перечитать подтверждённые пары
                    app.answer_cache.invalidate()         # сохранённые ответы могли не учитывать противоречие
                    return self._json(200, {"ok": True})
                if self.path == "/api/conflicts/import":   # отчёт ночного анализатора (conflicts.json) → кандидаты
                    return self._json(200, app.conflict_reg.import_report(b, app.windows, b.get("source") or "анализатор противоречий"))
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
    if "WARM" not in OFF and "FAST" not in OFF:      # прогрев частых вопросов из журнала — в фоне, запуск не задерживает
        threading.Thread(target=lambda: print(f"warm: {app.warm()} вопросов готовы", flush=True), daemon=True).start()
    print(f"assistant on :{a.port} mock={a.mock} windows={len(app.windows)}", flush=True)
    ThreadingHTTPServer((a.host, a.port), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
