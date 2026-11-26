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
    def search(q, allowed, k=16):
        return [w for w in windows if allowed is None or w["doc_id"] in allowed][:k]
    return search


WINS = [win("tech", "1.5", "Расчёт инициируют за две недели до подачи конкурса.", 0.9),
        win("dbr", "1.5", "Заявку подают не менее чем за три рабочих дня до предложения.", 0.8),
        win("ib_pre", "2.1", "Заявку на расчёт создаёт SS в CRM.", 0.5)]
WINS_EXT = [WINS[0], win("dbr", "1.5", "Заявку подают не менее чем за три рабочих дня до предложения.", 0.8,
                         ["Регламент подбора субподрядчика"]), WINS[2]]


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
        t = run_turn("За сколько запускать заявку на расчёт?", REG, search_for(WINS_EXT), llm, session={})
        self.assertEqual(t.outcome, "answer_with_caveat")          # пункт отсылает к документу вне базы
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
        self.assertFalse(FIND_DOC.search("Работы сданы. Какие закрывающие документы оформлять с клиентом?"))
        for q in ["Какой документ описывает назначение TPS?", "В каком документе есть исключение RFI?",
                  "Какие два документа отдельно описывают подготовку расчёта?", "Где найти правила риск-фонда?"]:
            self.assertTrue(FIND_DOC.search(q), q)
        t = run_turn("ДБРЦТ: где расписана работа с подрядчиком?", REG, search_for(WINS_EXT), FakeLLM({}), session={})
        self.assertEqual(t.outcome, "external_ref")

    def test_out_of_scope_margin_choice(self):
        self.assertTrue(OUT_OF_SCOPE.search("Мне для этой сделки лучше поставить маржу 18% или 25%?"))
        self.assertFalse(OUT_OF_SCOPE.search("Пресейл ИБ: проверяем порог 30% для услуг без хардоффера"))


class DialogTest(unittest.TestCase):
    """Режим диалога (кейс 2): уточнение условия; кейс 1: случай не оговорён → письмо автоматически."""
    ASK = {"ДБРЦТ": {"ask": "Предложение уже подано?", "ask_options": ["Предложение подано", "Предложения ещё нет", "Не определено, нужна экспертиза"]}}

    def test_condition_ask_then_answer(self):
        llm = FakeLLM({"ДБРЦТ": "answer"}, extra=self.ASK)
        sess = {}
        q = "ДБРЦТ: можно стартовать до договора?"
        t1 = run_turn(q, REG, search_for(WINS), llm, session=sess)
        self.assertEqual(t1.outcome, "ask")
        self.assertEqual([o["label"] for o in t1.options], ["Предложение подано", "Предложения ещё нет", "Не знаю"])  # вариант не из пункта отсечён
        sess["ask_reply"] = "Предложение подано"
        t2 = run_turn(q, REG, search_for(WINS), llm, session=sess)
        self.assertEqual(t2.outcome, "answer")
        self.assertIn("Уточнение сотрудника: Предложение уже подано? — Предложение подано", llm.calls[-1][1])
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

    def test_complex_project_no_auto_letter(self):
        nc = {"case_covered": False}
        llm = FakeLLM({"Техническая дирекция": "answer_with_caveat", "ДБРЦТ": "answer"}, extra={"Техническая дирекция": nc})
        t = run_turn("Общий конкурс ДБРЦТ и Техдирекции: за сколько запускать расчёт?", REG, search_for(WINS), llm, session={})
        self.assertFalse(t.route and t.route.get("email"))

    def test_conflict_sends_letter(self):
        t = run_turn("ДБРЦТ: можно стартовать до договора?", REG, search_for(WINS), FakeLLM({"ДБРЦТ": "conflict"}), session={})
        self.assertEqual(t.outcome, "conflict")
        self.assertEqual(t.route["why"], "противоречие между пунктами регламента")

    def test_practice_covers_case(self):
        prac = {**win("dbr", "P1", "Старт по гарантийному письму согласует директор дирекции.", 0.9), "kind": "practice"}
        llm = FakeLLM({"ДБРЦТ": "answer_with_caveat"}, extra={"ДБРЦТ": {"case_covered": False}})
        t = run_turn("ДБРЦТ: можно стартовать по гарантийному письму?", REG, search_for([prac] + WINS), llm, session={})
        self.assertFalse(t.route and t.route.get("email"))

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


class UniversalTest(unittest.TestCase):
    """Универсальная версия: тип вопроса от модели, большая база, бюджет контекста, кэш ответов, новое подразделение."""

    class IntentLLM(FakeLLM):
        def __init__(self, intent, answers=None, **kw):
            super().__init__(answers or {}, **kw)
            self.intent = intent

        def chat_json(self, system, user, schema, max_tokens=1500):
            if "готовишь поиск" in system:
                return {"queries": ["запрос"], "departments": [], "stage": "", "vague": False, "intent": self.intent}, {}
            return super().chat_json(system, user, schema, max_tokens)

    def test_intent_task_and_find_document(self):
        t = run_turn("Подготовь мне письмо заказчику", REG, search_for(WINS), self.IntentLLM("task"), session={})
        self.assertEqual(t.outcome, "task_refusal")
        t = run_turn("Где у нас про сроки расчёта?", REG, search_for(WINS), self.IntentLLM("find_document"), session={})
        self.assertEqual(t.outcome, "document")

    def test_out_of_scope_intent_overridden_by_strong_support(self):
        llm = self.IntentLLM("out_of_scope", {"Техническая дирекция": "answer"})
        t = run_turn("За сколько запускать расчёт конкурса?", REG, search_for(WINS), llm, session={"no_clarify": True})
        self.assertNotEqual(t.outcome, "out_of_scope")           # лучший пункт 0,9 ≥ 0,5 — отвечаем
        weak = [dict(w, score=0.2) for w in WINS]
        t = run_turn("Какой ноутбук купить?", REG, search_for(weak), self.IntentLLM("out_of_scope"), session={})
        self.assertEqual(t.outcome, "out_of_scope")

    def test_large_base_bounded(self):
        reg, wins = [], []
        for i in range(12):                                     # 12 подразделений × 3 документа
            for j in range(3):
                d = f"d{i}_{j}"
                reg.append({"doc_id": d, "department": f"DEP{i}", "stage": "full_cycle", "department_name": f"Дирекция {i}",
                            "display_title": d})
                DEPT[d] = f"DEP{i}"
                wins.append(win(d, f"{j}.1", f"Заявку подают за {i + 1} рабочих дня до предложения.", 0.9 - i * 0.03))
        calls = []

        def search(q, allowed, k=16):
            calls.append(allowed)
            return [w for w in wins if allowed is None or w["doc_id"] in allowed][:k]
        answers = {f"Дирекция {i}": "answer" for i in range(12)}
        llm = FakeLLM(answers)
        t = run_turn("За сколько подавать заявку?", reg, search, llm, session={"no_clarify": True})
        self.assertEqual(len(t.answers), 3)                     # не больше MAX_DEPTS
        self.assertLessEqual(len(calls), 2 * (1 + 3 * 3))       # 2 формулировки × (общий + ≤ 3 дирекции × 3 документа); без предела было бы 2 × 36
        self.assertEqual(calls[0], None)                        # сначала — вся база

    def test_context_budget_in_chars(self):
        from app.pipeline import CTX_CHARS, budget
        long = [dict(win("tech", f"{i}", "а" * 2400, 0.9 - i * 0.01)) for i in range(18)]
        ws = budget(long)
        self.assertLessEqual(sum(len(w["text"]) for w in ws), CTX_CHARS)
        self.assertLess(len(ws), 18)

    def test_answer_cache_between_users(self):
        from app.answer.cache import AnswerCache
        cache = AnswerCache()
        llm = FakeLLM({"ДБРЦТ": "answer"})
        run_turn("ДБРЦТ: за сколько подавать заявку?", REG, search_for(WINS), llm, session={}, answer_cache=cache)
        n = len(llm.calls)
        t = run_turn("ДБРЦТ:  за сколько подавать заявку", REG, search_for(WINS), llm, session={}, answer_cache=cache)
        self.assertEqual(len(llm.calls), n)                     # другой пользователь, тот же вопрос — без модели
        self.assertTrue(t.answers[0]["meta"].get("from_cache"))
        cache.invalidate()                                      # пересборка индекса — кэш сброшен
        run_turn("ДБРЦТ: за сколько подавать заявку?", REG, search_for(WINS), llm, session={}, answer_cache=cache)
        self.assertGreater(len(llm.calls), n)

    def test_new_department_without_code(self):
        reg = REG + [{"doc_id": "law", "department": "LAW", "stage": "full_cycle", "department_name": "Юридический департамент",
                      "display_title": "Юр"}]
        DEPT["law"] = "LAW"
        ws = WINS + [win("law", "2.1", "Договор согласует юрист за два рабочих дня.", 0.95)]
        llm = FakeLLM({"Юридический департамент": "answer"})
        t = run_turn("Юридический департамент: кто согласует договор?", reg, search_for(ws), llm, session={})
        self.assertEqual([a["label"] for a in t.answers], ["Юридический департамент"])


class KnownConflictTest(unittest.TestCase):
    def test_answer_on_conflicting_clause_becomes_conflict_with_letter(self):
        from app.pipeline import load_conflicts
        import json
        import tempfile
        from pathlib import Path
        f = Path(tempfile.mkdtemp()) / "c.json"
        f.write_text(json.dumps({"pairs": [{"department": "DBRCT", "a": "1.5", "b": "1.6", "why": "разные сроки"}]}))
        ws = WINS + [win("dbr", "1.6", "Заявку подают за пять рабочих дней.", 0.3)]
        conf = load_conflicts(ws, f)
        t = run_turn("ДБРЦТ: за сколько подавать заявку?", REG, search_for(WINS), FakeLLM({"ДБРЦТ": "answer"}), session={}, conflicts=conf)
        self.assertEqual(t.outcome, "conflict")
        self.assertEqual(t.answers[0]["result"]["conflict_with"][0]["clause"], "1.6")
        self.assertTrue(t.route["email"])


class SameAcrossTest(unittest.TestCase):
    class SameLLM(FakeLLM):
        def __init__(self, same, *a, **kw):
            super().__init__(*a, **kw)
            self.same = same

        def chat_json(self, system, user, schema, max_tokens=1500):
            if "Сравни ответы" in system:
                return {"same": self.same, "difference": "" if self.same else "разные сроки"}, {}
            return super().chat_json(system, user, schema, max_tokens)

    def test_same_answers_no_clarify(self):
        llm = self.SameLLM(True, {"Техническая дирекция": "answer", "ДБРЦТ": "answer"})
        t = run_turn("Кто подаёт заявку на расчёт?", REG, search_for(WINS), llm, session={})
        self.assertNotEqual(t.outcome, "clarify")
        self.assertEqual(len(t.answers), 2)
        self.assertIn("одинаков", t.text)

    def test_different_answers_clarify(self):
        llm = self.SameLLM(False, {"Техническая дирекция": "answer", "ДБРЦТ": "answer"})
        t = run_turn("За сколько подавать заявку на расчёт?", REG, search_for(WINS), llm, session={})
        self.assertEqual(t.outcome, "clarify")
