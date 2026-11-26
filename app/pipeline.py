"""Один ход диалога: тип вопроса → поиск → уточнение или ответ → письмо в СК при отсутствии опоры.

Исходы (инвариант «ни с чем не уходит», SPEC п. 88):
  answer / answer_with_caveat / conflict — ответ + источники;
  clarify — вопрос с вариантами (подразделения из выдачи + «несколько» + «не знаю»);
  escalate — опоры нет: письмо в службу качества, сотрудник в копии;
  out_of_scope / task_refusal — не про регламенты: отказ + куда обратиться.
"""
import re
from dataclasses import dataclass, field

from app.answer.core import answer as llm_answer
from app.dialog.clarify import decide, label

TASK_VERBS = re.compile(r"^\s*(сделай|напиши|составь|подготовь|проанализируй|переведи|сгенерируй|придумай|оформи|"
                        r"сформулируй|посчитай мне|нарисуй)\b", re.I)
OUT_OF_SCOPE = re.compile(r"\b(марж\w*\s+(по|моей|на)\s|преми\w*|бонус\w*|kpi|курс\s+(доллар|евро|валют)\w*|"
                          r"конкурент\w*|зарплат\w*|отпуск\w*|больничн\w*)", re.I)
NO_SUPPORT = 0.30          # скор реранкера лучшего окна ниже порога → опоры нет (подобрать на эталоне)


@dataclass
class Turn:
    outcome: str
    text: str = ""
    options: list = field(default_factory=list)
    answers: list = field(default_factory=list)      # по подразделению: {doc_id, label, result}
    route: dict = None
    trace: list = field(default_factory=list)
    hits: list = field(default_factory=list)


def route_for(question):
    q = question.lower()
    if re.search(r"марж|преми|бонус|kpi|зарплат", q):
        return {"to": "ваш руководитель", "why": "вопросы мотивации и экономики сделки не описаны в регламентах"}
    return {"to": "служба качества", "why": "вопрос не относится к регламентам проектной деятельности"}


def run_turn(question, registry, search_fn, llm, session=None):
    """search_fn(question, allowed) -> hits (со score реранкера). session: {'docs': [...], 'clarifications': n}."""
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
    allowed = set(session.get("docs") or []) or None
    hits = search_fn(question, allowed)
    best = hits[0]["score"] if hits else 0
    tr.append(f"поиск: {len(hits)} фрагментов, лучший скор {best:.2f}")
    if best < NO_SUPPORT:
        tr.append("опоры в регламентах нет → письмо в службу качества")
        return Turn("escalate", "В регламентах нет ответа на этот вопрос. Я отправил вопрос в службу качества, вы в копии — ответ придёт на почту.",
                    route={"to": "служба качества", "email": True}, trace=tr, hits=hits)
    d = decide(question, hits, registry, session_docs=session.get("docs"))
    tr.append(f"подразделение: {d.reason}")
    if d.action == "clarify" and session.get("clarifications", 0) < 2:
        return Turn("clarify", "Порядок зависит от подразделения. Какое вас интересует?",
                    options=d.options + [{"doc_id": "*", "label": "Несколько (комплексный проект)"},
                                         {"doc_id": "?", "label": "Не знаю"}], trace=tr, hits=hits)
    by_id = {x["doc_id"]: x for x in registry}
    docs = d.doc_ids or [hits[0]["doc_id"]]
    results = []
    for doc_id in docs:
        ws = [h for h in hits if h["doc_id"] == doc_id][:8]
        if not ws:
            ws = search_fn(question, {doc_id})[:8]
        doc = by_id[doc_id]
        ws = [{**w, "department_name": label(doc, registry), "doc_title": doc["display_title"]} for w in ws]
        res, meta = llm_answer(llm, question, ws, label(doc, registry))
        results.append({"doc_id": doc_id, "label": label(doc, registry), "result": res, "meta": meta})
        tr.append(f"ответ {label(doc, registry)}: {res['verdict']}, шагов {len(res['steps'])}, отклонено {len(res['rejected'])}")
    verdicts = [r["result"]["verdict"] for r in results]
    if all(v == "no_answer" for v in verdicts):
        tr.append("модель не нашла ответа во фрагментах → письмо в службу качества")
        return Turn("escalate", "В регламентах прямого ответа нет. Я отправил вопрос в службу качества, вы в копии.",
                    route={"to": "служба качества", "email": True}, answers=results, trace=tr, hits=hits)
    outcome = "conflict" if "conflict" in verdicts else ("answer_with_caveat" if "answer_with_caveat" in verdicts else "answer")
    return Turn(outcome, answers=results, trace=tr, hits=hits)
