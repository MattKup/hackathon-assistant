"""Решение об уточнении по verdict модели (разбор v4, исправление 1), кэш второго хода, no_clarify,
проверка «вне регламентов» перед письмом (4), внешние документы по процитированным пунктам (6), FIND_DOC (7)."""
import re
import unittest

from app.answer import core
from app.pipeline import FIND_DOC, OUT_OF_SCOPE, run_turn

REG = [
    {"doc_id": "ib_pre", "department": "IB", "stage": "presale", "department_name": "Дирекция ИБ", "display_title": "ИБ пресейл"},
    {"doc_id": "ib_prj", "department": "IB", "stage": "project", "department_name": "Дирекция ИБ", "display_title": "ИБ проект"},
    {"doc_id": "dbr", "department": "DBRCT", "stage": "full_cycle", "department_name": "ДБРЦТ", "display_title": "ДБРЦТ"},
    {"doc_id": "tech", "department": "TECH", "stage": "full_cycle", "department_name": "Техническая дирекция", "display_title": "Тех"},
]
DEPT = {"ib_pre": "IB", "ib_prj": "IB", "dbr": "DBRCT", "tech": "TECH"}


def win(doc, clause, text, score, ext=()):
    return {"window_id": f"{doc}-{clause}", "doc_id": doc, "department": DEPT[doc], "clause": clause, "section": "1",
            "section_title": f"Раздел {doc} {clause}", "text": text, "score": score, "external_refs": list(ext), "path": doc}


class FakeLLM:
    """answers: {«Техническая дирекция»: verdict}; scope: in_scope для классификатора темы."""

    def __init__(self, answers, scope=True, vague=False, extra=None):
        self.answers, self.scope, self.calls, self.vague, self.extra = answers, scope, [], vague, extra or {}

    def chat_json(self, system, user, schema, max_tokens=1500):
        if "готовишь поиск" in system:
            return {"queries": ["запрос"], "departments": [], "stage": "", "vague": self.vague}, {"prompt_tokens": 100, "completion_tokens": 10}
        if "классифицируешь" in system:
            return {"in_scope": self.scope}, {}
        dept = re.search(r"Подразделение: (.+)", user).group(1).strip()
        self.calls.append((dept, user))
        v = self.answers.get(dept, "no_answer")
        if v == "no_answer":
            return {"verdict": "no_answer", "summary": "", "steps": [], "caveat": "", "missing": "нет"}, {}
        text = re.search(r"\[w1\][^\n]*\n(.+)", user).group(1)
        extra = {} if "Уточнение сотрудника" in user else self.extra.get(dept, {})
        return {"verdict": v, "summary": "ответ", "caveat": "", "missing": "", "case_covered": True, "ask": "", "ask_options": [],
                "steps": [{"text": "шаг", "who": "", "deadline": "", "ref": "w1", "quote": text[:40]}], **extra}, {}


def search_for(windows):
    def search(q, allowed):
        return [w for w in windows if w["doc_id"] in allowed]
    return search


WINS = [win("tech", "1.5", "Расчёт инициируют за две недели до подачи конкурса.", 0.9),
        win("dbr", "1.5", "Заявку подают не менее чем за три рабочих дня до предложения.", 0.8, ["Регламент подбора субподрядчика"]),
        win("ib_pre", "2.1", "Заявку на расчёт создаёт SS в CRM.", 0.5)]


class VerdictFlowTest(unittest.TestCase):
    def test_two_answered_clarify_then_cached(self):
        llm = FakeLLM({"Техническая дирекция": "answer", "ДБРЦТ": "answer"})
        sess = {}
        t1 = run_turn("За сколько запускать заявку на расчёт?", REG, search_for(WINS), llm, session=sess)
        self.assertEqual(t1.outcome, "clarify")
        self.assertEqual([o["doc_id"] for o in t1.options[:2]], ["TECH", "DBRCT"])   # только ответившие дирекции
        n = len(llm.calls)
        sess.update(docs=["dbr"], clarifications=1)
        t2 = run_turn("За сколько запускать заявку на расчёт?", REG, search_for(WINS), llm, session=sess)
        self.assertEqual(len(llm.calls), n)                        # второй ход из кэша, без LLM
        self.assertEqual([a["label"] for a in t2.answers], ["ДБРЦТ"])
        self.assertEqual(t2.outcome, "answer")

    def test_no_clarify_answers_all(self):
        llm = FakeLLM({"Техническая дирекция": "answer", "ДБРЦТ": "answer"})
        t = run_turn("За сколько запускать заявку на расчёт?", REG, search_for(WINS), llm, session={"no_clarify": True})
        self.assertEqual(t.outcome, "answer")
        self.assertEqual({a["label"] for a in t.answers}, {"Техническая дирекция", "ДБРЦТ"})
        self.assertIn("Дирекция ИБ", t.text)                       # «в остальных не сказано»

    def test_one_answered_no_clarify_note_others(self):
        llm = FakeLLM({"ДБРЦТ": "answer"})
        t = run_turn("За сколько запускать заявку на расчёт?", REG, search_for(WINS), llm, session={})
        self.assertEqual(t.outcome, "answer")
        self.assertEqual([a["label"] for a in t.answers], ["ДБРЦТ"])
        self.assertIn("не сказано", t.text)
        self.assertEqual(t.external_refs, ["Регламент подбора субподрядчика"])   # процитирован п. ДБРЦТ 1.5

    def test_all_no_answer_escalate_or_out_of_scope(self):
        t = run_turn("Сколько лет хранить документацию проекта?", REG, search_for(WINS), FakeLLM({}), session={})
        self.assertEqual(t.outcome, "escalate")
        t = run_turn("Какой ноутбук купить домой для игр?", REG, search_for(WINS), FakeLLM({}, scope=False), session={})
        self.assertEqual(t.outcome, "out_of_scope")

    def test_budget_exhausted_answers_all(self):
        llm = FakeLLM({"Техническая дирекция": "answer", "ДБРЦТ": "answer"})
        t = run_turn("За сколько запускать заявку на расчёт?", REG, search_for(WINS), llm, session={"clarifications": 2})
        self.assertEqual(len(t.answers), 2)

    def test_complex_question_scoped_per_department(self):
        llm = FakeLLM({"Техническая дирекция": "answer", "ДБРЦТ": "answer"})
        t = run_turn("Общий конкурс ДБРЦТ и Техдирекции: за сколько запускать расчёт каждой части?", REG, search_for(WINS), llm, session={})
        self.assertEqual(len(t.answers), 2)
        self.assertTrue(all("части работ" in u for _, u in llm.calls))

    def test_find_doc_wider_and_external(self):
        self.assertTrue(FIND_DOC.search("В каких из доступных документов написано, что риски отражают в реестре?"))
        self.assertTrue(FIND_DOC.search("Где расписана специальная работа с подрядчиком?"))
        t = run_turn("ДБРЦТ: где расписана работа с подрядчиком?", REG, search_for(WINS), FakeLLM({}), session={})
        self.assertEqual(t.outcome, "external_ref")

    def test_out_of_scope_margin_choice(self):
        self.assertTrue(OUT_OF_SCOPE.search("Мне для этой сделки лучше поставить маржу 18% или 25%?"))
        self.assertFalse(OUT_OF_SCOPE.search("Пресейл ИБ: проверяем порог 30% для услуг без хардоффера"))


class DialogTest(unittest.TestCase):
    """Режим диалога (кейс 2): уточнение условия; кейс 1: случай не оговорён → письмо автоматически."""
    ASK = {"ДБРЦТ": {"ask": "Есть подписанное гарантийное письмо?", "ask_options": ["Есть", "Нет"]}}

    def test_condition_ask_then_answer(self):
        llm = FakeLLM({"ДБРЦТ": "answer"}, extra=self.ASK)
        sess = {}
        q = "ДБРЦТ: можно стартовать до договора?"
        t1 = run_turn(q, REG, search_for(WINS), llm, session=sess)
        self.assertEqual(t1.outcome, "ask")
        self.assertEqual([o["label"] for o in t1.options], ["Есть", "Нет", "Не знаю"])
        sess["ask_reply"] = "Есть"
        t2 = run_turn(q, REG, search_for(WINS), llm, session=sess)
        self.assertEqual(t2.outcome, "answer")
        self.assertIn("Уточнение сотрудника: Есть подписанное гарантийное письмо? — Есть", llm.calls[-1][1])
        self.assertNotIn("ask", sess)

    def test_condition_dont_know(self):
        llm = FakeLLM({"ДБРЦТ": "answer"}, extra=self.ASK)
        sess = {}
        q = "ДБРЦТ: можно стартовать до договора?"
        run_turn(q, REG, search_for(WINS), llm, session=sess)
        n = len(llm.calls)
        sess["ask_reply"] = "Не знаю"
        t = run_turn(q, REG, search_for(WINS), llm, session=sess)
        self.assertEqual(t.outcome, "answer_with_caveat")
        self.assertIn("однозначно ответить нельзя", t.text)
        self.assertEqual(len(llm.calls), n)

    def test_no_clarify_never_asks(self):
        llm = FakeLLM({"ДБРЦТ": "answer"}, extra=self.ASK)
        t = run_turn("ДБРЦТ: можно стартовать до договора?", REG, search_for(WINS), llm, session={"no_clarify": True})
        self.assertEqual(t.outcome, "answer")

    def test_case_not_covered_sends_letter(self):
        llm = FakeLLM({"Техническая дирекция": "answer_with_caveat"}, extra={"Техническая дирекция": {"case_covered": False}})
        t = run_turn("Техдирекция: скрин обещания оплатить — гарантия для старта?", REG, search_for(WINS), llm, session={})
        self.assertEqual(t.outcome, "answer_with_caveat")
        self.assertTrue(t.route and t.route.get("email"))
        self.assertTrue(t.answers)                                  # правило показываем

    def test_vague_offers_topics(self):
        sess = {}
        t = run_turn("а что со сроками?", REG, search_for(WINS), FakeLLM({}, vague=True), session=sess)
        self.assertEqual(t.outcome, "ask")
        self.assertEqual(t.options[-1]["label"], "Отправить вопрос в службу качества")
        sess["ask_reply"] = "Отправить вопрос в службу качества"
        self.assertEqual(run_turn("а что со сроками?", REG, search_for(WINS), FakeLLM({}), session=sess).outcome, "escalate")

    def test_tokens_counted(self):
        t = run_turn("ДБРЦТ: можно стартовать до договора?", REG, search_for(WINS), FakeLLM({"ДБРЦТ": "answer"}), session={})
        self.assertEqual(t.tokens["prompt_tokens"], 100)
        self.assertGreaterEqual(t.tokens["calls"], 2)


if __name__ == "__main__":
    unittest.main()
