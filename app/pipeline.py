"""Один ход диалога: тип вопроса → поиск → уточнение или ответ → письмо в СК при отсутствии опоры.

Исходы (инвариант «ни с чем не уходит», SPEC п. 88):
  answer / answer_with_caveat / conflict — ответ + источники;
  clarify — вопрос с вариантами (подразделения из выдачи + «несколько» + «не знаю»);
  escalate — опоры нет: письмо в службу качества, сотрудник в копии;
  out_of_scope / task_refusal — не про регламенты: отказ + куда обратиться.
"""
import re
from dataclasses import dataclass, field

from concurrent.futures import ThreadPoolExecutor

from app.answer.core import answer as llm_answer
from app.answer.understand import understand
from app.dialog.clarify import DEPT_ALIASES, decide, label, mentioned

TASK_VERBS = re.compile(r"^\s*(?:пожалуйста,?\s*)?(сделай|напиши|составь|подготовь|проанализируй|переведи|сгенерируй|придумай|"
                        r"оформи|сформулируй|посчитай|нарисуй|разбери|собери|открой|запроси|выпиши|отправь|создай|заполни|"
                        r"сократи|перескажи мне|набросай|проверь мой|исправь)\b|\bза меня\b", re.I)
# (7) шире: «в каких из доступных документов», «где расписана/написано/указано»
FIND_DOC = re.compile(r"\bв\s+как\w*(\s+[\w-]+){0,3}?\s+(документ|регламент)\w*|"
                      r"\bкак(ой|ие|ом)\s+(документ|регламент)\w*\s+(описыва|содерж|объясня|регулиру|расписыва|есть)\w*|"
                      r"\bгде(\s+[\w-]+){0,2}?\s+(описан|прописан|расписан|написан|указан|изложен|найти|лежит|почитать|посмотреть)\w*|"
                      r"\bкакие\s+(два\s+)?документ\w*(\s+[\w-]+)?\s+(описыва|содерж|отдельно)\w*", re.I)
OUT_OF_SCOPE = re.compile(r"\b(как\w*\s+марж\w*\s+постав|марж\w*\s+(по|моей|на)\s|преми\w*|бонус\w*|kpi|курс\s+(доллар|евро|валют)\w*|"
                          r"конкурент\w*|зарплат\w*|отпуск\w*|больничн\w*|постав\w*\s+марж\w*\s+\d|марж\w*\s+\d+\s*%\s+или)", re.I)
DEPT_NAMES = {"TECH": "Техническая дирекция", "IB": "Дирекция ИБ", "DBRCT": "ДБРЦТ"}
PER_DOC = 16      # фрагментов на документ после слияния запросов
PER_DEPT = 18     # фрагментов на дирекцию в контексте модели (документы маленькие, контекст позволяет)
CLARIFY_FLOOR = 0.15      # документ считается «с опорой», если его лучший скор ≥ max(FLOOR, RATIO × лучший общий)
CLARIFY_RATIO = 0.3
NO_SUPPORT = 0.05          # ниже — опоры нет (v1: 0.30, v2: 0.12 — терялись верные ответы; дальше решает verdict модели)


SCOPE_SYSTEM = ("Ты классифицируешь вопрос сотрудника. База знаний — регламенты проектной деятельности компании: "
                "пресейл, заявки на расчёт, ТКП/КП, договоры с заказчиком и субподрядчиками, сметы, старт, ведение, "
                "приёмка и закрытие проектов, роли (АМ, SS, РП, архитектор и т. п.). Ответь JSON: "
                "in_scope=true, если вопрос о том, как устроены эти процессы в компании (даже если ответа может не быть); "
                "Вопросы о правилах, сроках, порогах, санкциях, контактах, ролях и процедурах компании, о проведении проектных "
                "мероприятий (ретроспектива, приёмка, доступы к сайту проекта, статус сотрудников в проекте) — in_scope, даже если "
                "ответа в регламентах может не быть. false — только если вопрос о другом: экономика конкретной сделки сотрудника "
                "(какую маржу поставить, какая будет премия), текущие данные конкретной сделки или проекта (сколько денег осталось "
                "сейчас, какой статус у моей заявки), рынок и конкуренты, законы вообще, бытовое.")
SCOPE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["in_scope"],
                "properties": {"in_scope": {"type": "boolean"}}}


@dataclass
class Turn:
    outcome: str
    text: str = ""
    options: list = field(default_factory=list)
    answers: list = field(default_factory=list)      # по подразделению: {doc_id, label, result}
    route: dict = None
    trace: list = field(default_factory=list)
    hits: list = field(default_factory=list)
    documents: list = field(default_factory=list)     # для «где регламент»: [{title, label, sections}]
    external_refs: list = field(default_factory=list)
    tokens: dict = field(default_factory=dict)        # для расчёта экономики: вызовы и токены модели за ход


COMPLEX_NOTE = ("\n\n(Это комплексный проект нескольких дирекций. Ответь для части работ, которую ведёт {dept}, по её регламенту. "
                "То, что в регламенте нет правил о совместных проектах или о других дирекциях, — не причина для no_answer.)")


class Counted:
    """Считает вызовы и токены модели за ход (расчёт экономики — по ценам провайдера, решение организаторов 26.11)."""

    def __init__(self, llm):
        self.llm, self.t = llm, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def chat_json(self, *a, **kw):
        res, meta = self.llm.chat_json(*a, **kw)
        self.t["calls"] += 1
        self.t["prompt_tokens"] += (meta or {}).get("prompt_tokens") or 0
        self.t["completion_tokens"] += (meta or {}).get("completion_tokens") or 0
        return res, meta


ASK_DONT_KNOW = "Не знаю"
ASK_SEND = "Отправить вопрос в службу качества"
NOT_COVERED = "Ваш случай в регламенте прямо не оговорён — я отправил вопрос в службу качества, вы в копии. Ниже — правило, которое есть."

CONFLICT = ("Пункты регламента здесь противоречат друг другу — однозначного ответа нет. Я отправил вопрос в службу качества, "
            "вы в копии. Ниже — оба пункта.")


def outcome_of(results):
    verdicts = [r["result"]["verdict"] for r in results]
    return "conflict" if "conflict" in verdicts else ("answer_with_caveat" if "answer_with_caveat" in verdicts else "answer")


PRACTICE_COVERS = 0.5      # подтверждённая практика СК с таким скором в контексте дирекции — случай уже разобран экспертом


def practice_covers(r):
    """Кейс 3: после подтверждения эксперта тот же вопрос не должен снова уходить письмом в СК."""
    if any(st.get("doc_title", "").startswith("Практика") for st in r["result"].get("steps", [])):
        return True
    return any(w.get("kind") == "practice" and w.get("score", 0) >= PRACTICE_COVERS for w in r.get("windows") or [])


def not_covered(results, complex_=False):
    """Автописьмо «случай не оговорён» — только по ответу одной дирекции и не в комплексном проекте
    (v6: письмо уходило на каждую оговорку, в 10 из 15 комплексных проектов)."""
    shown = [r for r in results if r["result"]["verdict"] != "no_answer"]
    if complex_ or len(shown) != 1:
        return False
    r = shown[0]
    return r["result"]["verdict"] == "answer_with_caveat" and r["result"].get("case_covered") is False and not practice_covers(r)


def grounded_options(options, windows):
    """Варианты уточнения — только словами пункта (проверка кодом, как цитаты; v6 q148: «Не определено, требуется экспертиза»)."""
    from app.answer.core import stems
    text = stems(" ".join(w.get("text", "") for w in windows))
    return [o.strip() for o in options if o.strip() and stems(o) & text][:4]


def in_scope_llm(llm, question):
    try:
        sc, _ = llm.chat_json(SCOPE_SYSTEM, question, SCOPE_SCHEMA, max_tokens=20)
        return bool(sc.get("in_scope", True))
    except Exception:
        return True


def cited_refs(results):
    """(6) Внешние документы — только из пунктов, на которые опирается ответ."""
    return sorted({ref for r in results for st in r["result"].get("steps", []) for ref in st.get("external_refs", [])})


def plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def strip_departments(question):
    """Названия подразделений мешают поиску внутри документа одного подразделения."""
    q = question
    for pats in DEPT_ALIASES.values():
        for pat in pats:
            q = re.sub(pat, " ", q, flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"\s*\+\s*", " ", q)).strip() or question


def route_for(question):
    q = question.lower()
    if re.search(r"марж|преми|бонус|kpi|зарплат", q):
        return {"to": "ваш руководитель", "why": "вопросы мотивации и экономики сделки не описаны в регламентах"}
    return {"to": "служба качества", "why": "вопрос не относится к регламентам проектной деятельности"}


def _docs_for(depts, stage, registry):
    out = []
    for d in registry:
        if d["department"] not in depts:
            continue
        same = [x for x in registry if x["department"] == d["department"]]
        if len(same) > 1 and stage in ("presale", "project") and d["stage"] != stage:
            continue
        out.append(d["doc_id"])
    return out


def run_turn(question, registry, search_fn, llm, session=None, workers=4):
    """search_fn(query, allowed) -> hits (score реранкера).
    session (мутируется): docs, clarifications, no_clarify, complex; cache — ответы по дирекциям до выбора;
    ask — открытый уточняющий вопрос (условие или тема); ask_reply — ответ сотрудника на него."""
    session = {} if session is None else session
    c = Counted(llm)
    t = _run_turn(question, registry, search_fn, c, session, workers)
    t.tokens = c.t
    return t


def _ask_turn(session, kind, question, text, options, tr, hits, base=None, dep=None, ws=None):
    session["ask"] = {"kind": kind, "q": question, "text": text, "options": options, "base": base, "dep": dep, "ws": ws}
    return Turn("ask", text, options=[{"doc_id": f"a{i}", "label": o} for i, o in enumerate(options)], trace=tr, hits=hits)


def _final(results, note, tr, hits, session, question, complex_=False):
    """Ответ готов. Одна дирекция и ответ зависит от условия, не сказанного в вопросе, → уточняющий вопрос (кейс 2);
    правило есть, а случай не оговорён → ответ с оговоркой + автоматическое письмо в службу качества (кейс 1)."""
    shown = [r for r in results if r["result"]["verdict"] != "no_answer"]
    if len(shown) == 1 and not session.get("no_clarify") and session.get("clarifications", 0) < 2:
        r = shown[0]
        opts = grounded_options(r["result"].get("ask_options") or [], r.get("windows") or [])
        if r["result"].get("ask", "").strip() and len(opts) >= 2:
            session["clarifications"] = session.get("clarifications", 0) + 1
            tr.append(f"{r['label']}: ответ зависит от условия, которого нет в вопросе, — уточняю")
            t = _ask_turn(session, "condition", question, r["result"]["ask"].strip(), opts[:4] + [ASK_DONT_KNOW], tr, hits,
                          base=r, dep=r["doc_id"], ws=r.get("windows"))
            session["ask"]["hits"] = hits[:5]
            return t
    t = Turn(outcome_of(results), note, answers=results, trace=tr, hits=hits, external_refs=cited_refs(results))
    if t.outcome == "conflict":
        tr.append("Пункты регламента противоречат друг другу — отправляю вопрос в службу качества")
        t.text = CONFLICT + (" " + note if note else "")
        t.route = {"to": "служба качества", "email": True, "why": "противоречие между пунктами регламента"}
    elif not_covered(results, complex_):
        tr.append("Правило есть, но случай сотрудника в нём не оговорён — отправляю вопрос в службу качества")
        t.text = (NOT_COVERED + (" " + note if note else ""))
        t.route = {"to": "служба качества", "email": True, "why": "случай не оговорён"}
    return t


def _run_turn(question, registry, search_fn, llm, session, workers):
    tr = []
    if TASK_VERBS.search(question):
        tr.append("Это просьба выполнить работу, а не вопрос по регламентам — отказ")
        return Turn("task_refusal", "Я отвечаю только на вопросы по регламентам компании. Для таких задач в компании есть другие инструменты.",
                    route=route_for(question), trace=tr)
    if OUT_OF_SCOPE.search(question):
        tr.append("Вопрос не про регламенты — подсказываю, куда обратиться")
        r = route_for(question)
        return Turn("out_of_scope", f"Этот вопрос не касается базы регламентов. Обратитесь: {r['to']}.", route=r, trace=tr)

    by_id = {x["doc_id"]: x for x in registry}
    ask, reply = session.get("ask"), session.pop("ask_reply", None)
    if ask and reply is not None and ask["q"] == question:
        session.pop("ask", None)
        if ask["kind"] == "topic":
            if reply == ASK_SEND:
                tr.append("Сотрудник попросил отправить вопрос в службу качества")
                return Turn("escalate", "Я отправил вопрос в службу качества, вы в копии — ответ придёт на почту.",
                            route={"to": "служба качества", "email": True}, trace=tr)
            tr.append(f"Сотрудник уточнил тему: {reply}")
            return _run_turn(f"{question} ({reply})", registry, search_fn, llm, session, workers)
        base = ask["base"]
        if reply == ASK_DONT_KNOW:
            tr.append("Условие неизвестно — показываю правило со всеми условиями; однозначный ответ дать нельзя")
            return Turn("answer_with_caveat", "Без этого условия однозначно ответить нельзя. Ниже — правило и от чего оно зависит; "
                        "если условие неясно, задайте вопрос службе качества.", answers=[base], trace=tr,
                        hits=ask.get("hits") or [], external_refs=cited_refs([base]))
        tr.append(f"Сотрудник уточнил условие: {reply}")
        q2 = f"{question}\n\nУточнение сотрудника: {ask['text']} — {reply}"
        res, meta = llm_answer(llm, q2, ask["ws"], base["label"])
        r = {**base, "result": res, "meta": meta}
        if res["verdict"] == "no_answer":
            r = base
        tr.append(f"{r['label']}: ответ для варианта «{reply}»")
        out = Turn(outcome_of([r]), answers=[r], trace=tr, external_refs=cited_refs([r]))
        if not_covered([r]):
            out.text, out.route = NOT_COVERED, {"to": "служба качества", "email": True, "why": "случай не оговорён"}
        return out
    cache = session.get("cache")
    if cache and cache["q"] == question and session.get("docs"):
        chosen = {by_id[d]["department"] for d in session["docs"] if d in by_id}
        cached = [cache["answers"][d] for d in cache["order"] if d in chosen]
        if cached and chosen <= set(cache["answers"]):
            tr.append("Ответы по дирекциям уже готовы — показываю " + ("выбранную" if len(cached) == 1 else "выбранные"))
            hits = cache["hits"]
            return _final(cached, "", tr, hits, session, question, bool(session.get("complex")) or len(cached) > 1)

    # 1. понять вопрос (агентный шаг)
    u, _ = understand(llm, question)
    # исходная формулировка всегда в поиске: переписывание может увести в сторону (26.11: «заявка» → «РТЗ»)
    queries = list(dict.fromkeys([strip_departments(question)] + [strip_departments(x) for x in u["queries"]]))[:4]
    tr.append("Понял вопрос как: " + " · ".join(queries[1:] or queries))
    named = mentioned(question, registry) or _docs_for(set(u.get("departments") or []), u.get("stage", ""), registry)
    if named:
        tr.append("Дирекция названа в вопросе")
    scope = list(session.get("docs") or named or [d["doc_id"] for d in registry])

    # 2. поиск по каждому документу и каждому запросу — параллельно
    def doc_hits(doc_id):
        """Слияние выдач по запросам: RRF по рангам (а не максимум скора) — пункт, лучший для одного
        из запросов, не вытесняется пунктами с высоким скором по другим (26.11: ДБРЦТ п. 1.5)."""
        best, rrf = {}, {}
        for qq in queries:
            for r, h in enumerate(search_fn(qq, {doc_id})):
                wid = h["window_id"]
                rrf[wid] = rrf.get(wid, 0) + 1 / (10 + r)
                if wid not in best or h["score"] > best[wid]["score"]:
                    best[wid] = h
        order = sorted(best, key=lambda w: -rrf[w])[:PER_DOC]
        return doc_id, sorted((best[w] for w in order), key=lambda h: -h["score"])
    with ThreadPoolExecutor(max_workers=workers) as ex:
        per_doc = dict(ex.map(doc_hits, scope))
    hits = sorted([h for hs in per_doc.values() for h in hs], key=lambda h: -h["score"])
    best = hits[0]["score"] if hits else 0
    tr.append(f"Искал в {len(scope)} {plural(len(scope), 'регламенте', 'регламентах', 'регламентах')} по {len(queries)} {plural(len(queries), 'формулировке', 'формулировкам', 'формулировкам')}")

    def escalate_or_scope(results):
        """(4) Перед письмом — проверка «вопрос вообще про процессы компании?»."""
        ext = sorted(set(hits[0].get("external_refs", []))) if hits else []   # (6) только лучший пункт, не вся выдача
        if ext:
            tr.append("Пункт отсылает к документу, которого нет в базе, — называю его")
            return Turn("external_ref", "Порядок описан в отдельном документе, которого нет в базе ассистента: " + "; ".join(ext),
                        answers=results, trace=tr, hits=hits, external_refs=ext)
        if not in_scope_llm(llm, question):
            tr.append("Ответа нет, и вопрос не про процессы компании — подсказываю, куда обратиться")
            r = route_for(question)
            return Turn("out_of_scope", f"Этот вопрос не касается базы регламентов. Обратитесь: {r['to']}.", route=r, trace=tr, hits=hits)
        tr.append("В найденных пунктах ответа нет — отправляю вопрос в службу качества")
        return Turn("escalate", "В регламентах прямого ответа нет. Я отправил вопрос в службу качества, вы в копии.",
                    route={"to": "служба качества", "email": True}, answers=results, trace=tr, hits=hits)

    def _finish(results, note=""):
        if all(r["result"]["verdict"] == "no_answer" for r in results):
            return escalate_or_scope(results)
        return _final(results, note, tr, hits, session, question, complex_)

    if best < NO_SUPPORT:
        if not in_scope_llm(llm, question):
            tr.append("Подходящих пунктов нет, и вопрос не про процессы компании — подсказываю, куда обратиться")
            r = route_for(question)
            return Turn("out_of_scope", f"Этот вопрос не касается базы регламентов. Обратитесь: {r['to']}.", route=r, trace=tr, hits=hits)
        tr.append("Подходящих пунктов в регламентах нет — отправляю вопрос в службу качества")
        return Turn("escalate", "В регламентах нет ответа на этот вопрос. Я отправил вопрос в службу качества, вы в копии — ответ придёт на почту.",
                    route={"to": "служба качества", "email": True}, trace=tr, hits=hits)

    # вопрос не сопоставляется с регламентом (размытый) → сначала уточнить тему, а не отвечать наугад
    topics = list(dict.fromkeys(h["section_title"] for h in hits[:8] if h.get("section_title")))[:3]
    if u.get("vague") and len(topics) >= 2 and not session.get("no_clarify") and session.get("clarifications", 0) < 2:
        session["clarifications"] = session.get("clarifications", 0) + 1
        tr.append("Вопрос размытый — предлагаю близкие темы из регламентов")
        return _ask_turn(session, "topic", question, "Уточните, о чём вопрос:", topics + [ASK_SEND], tr, hits)

    if FIND_DOC.search(question):
        if hits[0].get("external_refs"):
            ext = sorted(set(hits[0]["external_refs"]))
            tr.append(f"Вопрос о том, где описано; лучший пункт ({hits[0]['clause']}) отсылает к документу, которого нет в базе")
            return Turn("external_ref", f"Пункт {hits[0]['clause']} отсылает к отдельному документу, которого нет в базе ассистента: "
                        + "; ".join(ext), trace=tr, hits=hits, external_refs=ext)
        docs_found = []
        for h in hits[:6]:
            if h["doc_id"] not in [d["doc_id"] for d in docs_found] and h["doc_id"] in by_id:
                docs_found.append({"doc_id": h["doc_id"], "title": by_id[h["doc_id"]]["display_title"],
                                   "label": label(by_id[h["doc_id"]], registry), "sections": []})
            dd = next((d for d in docs_found if d["doc_id"] == h["doc_id"]), None)
            sec = f"раздел {h['section']} «{h['section_title']}», п. {h['clause']}"
            if dd and sec not in dd["sections"]:
                dd["sections"].append(sec)
        tr.append(f"Вопрос о том, где описано, — нашёл {len(docs_found)} {plural(len(docs_found), 'документ', 'документа', 'документов')}")
        return Turn("document", "Это описано здесь:", documents=docs_found, trace=tr, hits=hits)

    # 3. дирекции, для которых готовим ответ: названные/выбранные или все, где есть опора (стадию система выбирает сама)
    dept_best = {}
    for d, hs in per_doc.items():
        if hs:
            dep = by_id[d]["department"]
            dept_best[dep] = max(dept_best.get(dep, 0), hs[0]["score"])
    fixed = bool(session.get("docs") or named)
    if fixed:
        depts = sorted({by_id[d]["department"] for d in scope}, key=lambda x: -dept_best.get(x, 0))
        tr.append("Дирекция " + ("выбрана вами" if session.get("docs") else "названа в вопросе"))
    else:
        depts = [dep for dep, sc in sorted(dept_best.items(), key=lambda kv: -kv[1]) if sc >= NO_SUPPORT]
        tr.append(f"Готовлю ответ по каждой дирекции, где нашлись пункты: {len(depts)}")
    # (2) комплексный проект: каждая дирекция отвечает за свою часть
    complex_ = len(depts) >= 2 and (session.get("complex") or len({by_id[d]["department"] for d in named}) >= 2)

    # 4. ответ по каждой дирекции — параллельно; окна из всех её документов (пресейл/проект — внутри)
    def one(dep):
        ws = []
        for d in [x for x in scope if by_id[x]["department"] == dep] or [x["doc_id"] for x in registry if x["department"] == dep]:
            doc = by_id[d]
            for w in per_doc.get(d) or doc_hits(d)[1]:
                ws.append({**w, "department_name": DEPT_NAMES.get(dep, dep),
                           "doc_title": "Практика службы качества" if w.get("kind") == "practice" else doc["display_title"]})
        ws = sorted({w["window_id"]: w for w in ws}.values(), key=lambda w: -w["score"])[:PER_DEPT]
        q = question + (COMPLEX_NOTE.format(dept=DEPT_NAMES.get(dep, dep)) if complex_ else "")
        res, meta = llm_answer(llm, q, ws, DEPT_NAMES.get(dep, dep))
        meta["clauses_seen"] = [w["clause"] for w in ws]
        return {"doc_id": dep, "label": DEPT_NAMES.get(dep, dep), "result": res, "meta": meta, "windows": ws}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, depts))
    for r in results:
        res = r["result"]
        verdict = {"answer": "ответ", "answer_with_caveat": "ответ с оговоркой", "conflict": "противоречие",
                   "no_answer": "прямого ответа нет"}.get(res["verdict"], res["verdict"])
        n, rej = len(res["steps"]), len(res["rejected"])
        tr.append(f"{r['label']}: {verdict}" + (f", {n} {plural(n, 'шаг', 'шага', 'шагов')} со ссылками на пункты" if n else "")
                  + (f"; {rej} {plural(rej, 'утверждение отброшено', 'утверждения отброшены', 'утверждений отброшено')} проверкой цитат" if rej else "; все цитаты сверены с текстом"))
        r["meta"]["trace_debug"] = f"пункты в контексте: {', '.join(r['meta'].get('clauses_seen', []))}"
    if fixed:
        return _finish(results)

    # 5. решение об уточнении — по verdict модели (разбор v4, исправление 1)
    answered = [r for r in results if r["result"]["verdict"] != "no_answer"]
    silent = [DEPT_NAMES.get(d, d) for d in DEPT_NAMES if d not in {r["doc_id"] for r in answered}]
    note = ("В регламентах " + (", ".join(silent[:-1]) + " и " + silent[-1] if len(silent) > 1 else silent[0])
            + " об этом не сказано.") if answered and silent else ""
    if len(answered) >= 2 and not session.get("no_clarify") and session.get("clarifications", 0) < 2:
        session["cache"] = {"q": question, "answers": {r["doc_id"]: r for r in answered}, "order": [r["doc_id"] for r in answered],
                            "hits": hits[:5]}
        tr.append(f"Ответ есть в регламентах {len(answered)} {plural(len(answered), 'дирекции', 'дирекций', 'дирекций')} и отличается — уточняю дирекцию")
        opts = [{"doc_id": r["doc_id"], "department": r["doc_id"], "label": r["label"]} for r in answered]
        return Turn("clarify", "Ответ зависит от дирекции. Какая дирекция ведёт проект?",
                    options=opts + [{"doc_id": "*", "label": "Проект нескольких дирекций"}, {"doc_id": "?", "label": "Не знаю"}],
                    trace=tr, hits=hits)
    return _finish(answered or results, note)
