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
FIND_DOC = re.compile(r"(как\w*|в как\w*)\s+(документ|регламент)\w*|где\s+(описан|прописан|найти|лежит|почитать)\w*|"
                      r"\bкакие\s+(два|документы)\b", re.I)
OUT_OF_SCOPE = re.compile(r"\b(как\w*\s+марж\w*\s+постав|марж\w*\s+(по|моей|на)\s|преми\w*|бонус\w*|kpi|курс\s+(доллар|евро|валют)\w*|"
                          r"конкурент\w*|зарплат\w*|отпуск\w*|больничн\w*)", re.I)
DEPT_NAMES = {"TECH": "Техническая дирекция", "IB": "Дирекция ИБ", "DBRCT": "ДБРЦТ"}
CLARIFY_FLOOR = 0.15      # документ считается «с опорой», если его лучший скор ≥ max(FLOOR, RATIO × лучший общий)
CLARIFY_RATIO = 0.3
NO_SUPPORT = 0.05          # ниже — опоры нет (v1: 0.30, v2: 0.12 — терялись верные ответы; дальше решает verdict модели)


SCOPE_SYSTEM = ("Ты классифицируешь вопрос сотрудника. База знаний — регламенты проектной деятельности компании: "
                "пресейл, заявки на расчёт, ТКП/КП, договоры с заказчиком и субподрядчиками, сметы, старт, ведение, "
                "приёмка и закрытие проектов, роли (АМ, SS, РП, архитектор и т. п.). Ответь JSON: "
                "in_scope=true, если вопрос о том, как устроены эти процессы в компании (даже если ответа может не быть); "
                "Вопросы о правилах, сроках, порогах, санкциях, контактах, ролях и процедурах компании — in_scope, даже если "
                "ответа в регламентах может не быть. false — только если вопрос о другом: экономика конкретной сделки сотрудника "
                "(какую маржу поставить, какая будет премия), рынок и конкуренты, законы вообще, бытовое.")
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
    """search_fn(query, allowed) -> hits (score реранкера). session: {'docs': [...], 'clarifications': n}."""
    session = session or {}
    tr = []
    if TASK_VERBS.search(question):
        tr.append("тип: просьба выполнить работу → отказ")
        return Turn("task_refusal", "Я отвечаю только на вопросы по регламентам компании. Для таких задач в компании есть другие инструменты.",
                    route=route_for(question), trace=tr)
    if OUT_OF_SCOPE.search(question):
        tr.append("тип: вне регламентов → отказ с маршрутом")
        r = route_for(question)
        return Turn("out_of_scope", f"Этот вопрос не касается базы регламентов. Обратитесь: {r['to']}.", route=r, trace=tr)

    # 1. понять вопрос (агентный шаг)
    u, _ = understand(llm, question)
    # исходная формулировка всегда в поиске: переписывание может увести в сторону (26.11: «заявка» → «РТЗ»)
    queries = list(dict.fromkeys([strip_departments(question)] + [strip_departments(x) for x in u["queries"]]))[:4]
    tr.append("разбор вопроса: " + " | ".join(queries))
    named = mentioned(question, registry) or _docs_for(set(u.get("departments") or []), u.get("stage", ""), registry)
    if named:
        tr.append("подразделение названо в вопросе")
    scope = list(session.get("docs") or named or [d["doc_id"] for d in registry])

    # 2. поиск по каждому документу и каждому запросу — параллельно
    def doc_hits(doc_id):
        best = {}
        for qq in queries:
            for h in search_fn(qq, {doc_id}):
                if h["window_id"] not in best or h["score"] > best[h["window_id"]]["score"]:
                    best[h["window_id"]] = h
        return doc_id, sorted(best.values(), key=lambda h: -h["score"])[:8]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        per_doc = dict(ex.map(doc_hits, scope))
    hits = sorted([h for hs in per_doc.values() for h in hs], key=lambda h: -h["score"])
    best = hits[0]["score"] if hits else 0
    tr.append(f"поиск: {len(scope)} документ(а), {len(queries)} запрос(а), лучший скор {best:.2f}")
    by_id = {x["doc_id"]: x for x in registry}

    if best < NO_SUPPORT:
        try:
            sc, _ = llm.chat_json(SCOPE_SYSTEM, question, SCOPE_SCHEMA, max_tokens=20)
            in_scope = bool(sc.get("in_scope", True))
        except Exception:
            in_scope = True
        if not in_scope:
            tr.append("опоры нет, вопрос не про процессы компании → отказ с маршрутом")
            r = route_for(question)
            return Turn("out_of_scope", f"Этот вопрос не касается базы регламентов. Обратитесь: {r['to']}.", route=r, trace=tr, hits=hits)
        tr.append("опоры в регламентах нет → письмо в службу качества")
        return Turn("escalate", "В регламентах нет ответа на этот вопрос. Я отправил вопрос в службу качества, вы в копии — ответ придёт на почту.",
                    route={"to": "служба качества", "email": True}, trace=tr, hits=hits)

    if FIND_DOC.search(question):
        docs_found = []
        for h in hits[:6]:
            if h["doc_id"] not in [d["doc_id"] for d in docs_found] and h["doc_id"] in by_id:
                docs_found.append({"doc_id": h["doc_id"], "title": by_id[h["doc_id"]]["display_title"],
                                   "label": label(by_id[h["doc_id"]], registry), "sections": []})
            dd = next((d for d in docs_found if d["doc_id"] == h["doc_id"]), None)
            sec = f"раздел {h['section']} «{h['section_title']}», п. {h['clause']}"
            if dd and sec not in dd["sections"]:
                dd["sections"].append(sec)
        tr.append(f"тип: где регламент → {len(docs_found)} документ(а)")
        return Turn("document", "Это описано здесь:", documents=docs_found, trace=tr, hits=hits)

    # 3. уточнять ли дирекцию — по лучшему скору документов каждой дирекции (стадию система выбирает сама)
    dept_best = {}
    for d, hs in per_doc.items():
        if hs:
            dep = by_id[d]["department"]
            dept_best[dep] = max(dept_best.get(dep, 0), hs[0]["score"])
    if session.get("docs") or named:
        depts = sorted({by_id[d]["department"] for d in scope}, key=lambda x: -dept_best.get(x, 0))
        tr.append("дирекция: " + ("выбрана в диалоге" if session.get("docs") else "названа в вопросе"))
    else:
        floor = max(CLARIFY_FLOOR, CLARIFY_RATIO * best)
        depts = [dep for dep, sc in sorted(dept_best.items(), key=lambda kv: -kv[1]) if sc >= floor]
        tr.append(f"дирекций с опорой в регламентах: {len(depts)}")
        if len(depts) >= 2 and session.get("clarifications", 0) < 2:
            opts = [{"doc_id": dep, "department": dep, "label": DEPT_NAMES.get(dep, dep)} for dep in depts]
            return Turn("clarify", "В дирекциях это устроено по-разному. О какой дирекции речь?",
                        options=opts + [{"doc_id": "*", "label": "Проект нескольких дирекций"}, {"doc_id": "?", "label": "Не знаю"}],
                        trace=tr, hits=hits)
        depts = depts[:1] or [by_id[hits[0]["doc_id"]]["department"]]

    # 4. ответ по каждой дирекции — параллельно; окна из всех её документов (пресейл/проект — внутри)
    def one(dep):
        ws = []
        for d in [x for x in scope if by_id[x]["department"] == dep] or [x["doc_id"] for x in registry if x["department"] == dep]:
            doc = by_id[d]
            for w in per_doc.get(d) or doc_hits(d)[1]:
                ws.append({**w, "department_name": DEPT_NAMES.get(dep, dep),
                           "doc_title": "Практика службы качества" if w.get("kind") == "practice" else doc["display_title"]})
        ws = sorted({w["window_id"]: w for w in ws}.values(), key=lambda w: -w["score"])[:10]
        res, meta = llm_answer(llm, question, ws, DEPT_NAMES.get(dep, dep))
        meta["clauses_seen"] = [w["clause"] for w in ws]
        return {"doc_id": dep, "label": DEPT_NAMES.get(dep, dep), "result": res, "meta": meta}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, depts))
    for r in results:
        tr.append(f"ответ {r['label']}: {r['result']['verdict']}, шагов {len(r['result']['steps'])}, отклонено {len(r['result']['rejected'])}; "
                  f"пункты: {', '.join(r['meta'].get('clauses_seen', [])[:10])}")
    verdicts = [r["result"]["verdict"] for r in results]
    ext = sorted({ref for h in hits[:5] for ref in h.get("external_refs", [])})
    if all(v == "no_answer" for v in verdicts) and ext:
        tr.append("ответ стоит за отсылкой к документу вне базы → называем документ")
        return Turn("external_ref", "Порядок описан в отдельном документе, которого нет в базе ассистента: " + "; ".join(ext),
                    answers=results, trace=tr, hits=hits, external_refs=ext)
    if all(v == "no_answer" for v in verdicts):
        tr.append("в найденных пунктах ответа нет → письмо в службу качества")
        return Turn("escalate", "В регламентах прямого ответа нет. Я отправил вопрос в службу качества, вы в копии.",
                    route={"to": "служба качества", "email": True}, answers=results, trace=tr, hits=hits)
    outcome = "conflict" if "conflict" in verdicts else ("answer_with_caveat" if "answer_with_caveat" in verdicts else "answer")
    return Turn(outcome, answers=results, trace=tr, hits=hits, external_refs=ext)
