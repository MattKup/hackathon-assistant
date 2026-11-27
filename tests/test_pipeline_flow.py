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

    def __init__(self, answers, scope=True, vague=False, extra=None, dept_unknown=False):
        self.answers, self.scope, self.calls, self.vague, self.extra = answers, scope, [], vague, extra or {}
        self.dept_unknown = dept_unknown

    def chat_json(self, system, user, schema, max_tokens=1500):
        if "готовишь поиск" in system:
            return {"queries": ["запрос"], "departments": [], "stage": "", "vague": self.vague, "dept_unknown": self.dept_unknown}, \
                {"prompt_tokens": 100, "completion_tokens": 10}
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
        self.assertIn("Прямого ответа не нашёл", t.text)
        self.assertEqual(t.external_refs, ["Регламент подбора субподрядчика"])   # процитирован п. ДБРЦТ 1.5

    def test_all_no_answer_escalate_or_out_of_scope(self):
        t = run_turn("Сколько лет хранить документацию проекта?", REG, search_for(WINS), FakeLLM({}), session={})
        self.assertEqual(t.outcome, "escalate")
        weak = [{**w, "score": 0.1} for w in WINS]
        t = run_turn("Какой ноутбук купить домой для игр?", REG, search_for(weak), FakeLLM({}, scope=False), session={})
        self.assertEqual(t.outcome, "out_of_scope")

    def test_dept_unknown_in_question_compares_all(self):
        """Г2: сотрудник сам пишет «не знаю, чья дирекция» — ответы по всем, без повторного вопроса; режим — только на ход."""
        sess = {}
        llm = FakeLLM({"Техническая дирекция": "answer", "ДБРЦТ": "answer"}, dept_unknown=True)
        t = run_turn("Не знаю, чей контур: за сколько подавать заявку?", REG, search_for(WINS), llm, session=sess)
        self.assertNotEqual(t.outcome, "clarify")
        self.assertEqual(len(t.answers), 2)
        self.assertNotIn("no_clarify", sess)

    def test_strong_support_never_out_of_scope(self):
        """Г1: пункты по теме найдены (сильная опора), а модель не ответила — письмо в СК, а не «вне регламентов»
        по одному тексту вопроса (разговорная формулировка)."""
        t = run_turn("Короче, клиент торопит — сколько просить?", REG, search_for(WINS), FakeLLM({}, scope=False), session={})
        self.assertEqual(t.outcome, "escalate")

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

    def test_model_conflict_on_one_clause_is_caveat_not_letter(self):
        t = run_turn("ДБРЦТ: можно стартовать до договора?", REG, search_for(WINS), FakeLLM({"ДБРЦТ": "conflict"}), session={})
        self.assertEqual(t.outcome, "answer_with_caveat")      # противоречие не опёрто на два пункта (ревью Fable)
        self.assertFalse(t.route and t.route.get("email"))

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
        t = run_turn("Какой ноутбук купить?", REG, search_for(weak), self.IntentLLM("out_of_scope", scope=False), session={})
        self.assertEqual(t.outcome, "out_of_scope")
        # Г1 (ревью Fable): разбор сказал «вне», а классификатор темы — «про правила компании» → письмо в СК, не «вне»
        t = run_turn("Сколько платят суточные в командировке к клиенту?", REG, search_for(weak), self.IntentLLM("out_of_scope"), session={})
        self.assertEqual(t.outcome, "escalate")

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


def _registry_with_pair(ws, confirm=True, text_b="Заявку подают за пять рабочих дней."):
    """Реестр в памяти: кандидат DBRCT 1.5↔1.6 (как от анализатора), по желанию — подтверждён СК."""
    from app.analysis.registry import ConflictRegistry
    reg = ConflictRegistry(":memory:")
    rep = {"findings": [{"kind": "contradiction", "relation": "same_doc", "explanation": "разные сроки подачи заявки",
                         "a": {"doc_id": "dbr", "clause": "1.5", "quote": WINS[1]["text"][:30]},
                         "b": {"doc_id": "dbr", "clause": "1.6", "quote": text_b[:20]}}]}
    reg.import_report(rep, ws)
    pid = reg.pairs()[0]["pair_id"]
    if confirm:
        reg.decide(pid, "confirm", "Иванова (СК)", "сверено с текстом")
    return reg, pid


class KnownConflictTest(unittest.TestCase):
    def test_answer_on_conflicting_clause_becomes_conflict_with_letter(self):
        from app.pipeline import load_conflicts
        ws = WINS + [win("dbr", "1.6", "Заявку подают за пять рабочих дней.", 0.3)]
        reg, _ = _registry_with_pair(ws)
        conf = load_conflicts(ws, reg.confirmed())
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
        self.assertIn("ниже по каждой", t.text)

    def test_different_answers_clarify(self):
        llm = self.SameLLM(False, {"Техническая дирекция": "answer", "ДБРЦТ": "answer"})
        t = run_turn("За сколько подавать заявку на расчёт?", REG, search_for(WINS), llm, session={})
        self.assertEqual(t.outcome, "clarify")


class DeepPassTest(unittest.TestCase):
    """Агентный проход: ответ с оговоркой и «не хватает…» → добираем пункт по ссылке и соседний, отвечаем снова."""

    class DeepLLM(FakeLLM):
        def chat_json(self, system, user, schema, max_tokens=1500):
            if "Подразделение:" in user and "АМ направляет" not in user:   # первый проход: пункта 8.5 в контексте нет
                self.calls.append(("ДБРЦТ", user))
                text = re.search(r"\[w1\][^\n]*\n(.+)", user).group(1)
                return {"verdict": "answer_with_caveat", "summary": "частично", "caveat": "не всё", "missing": "порядок без письма",
                        "case_covered": True, "ask": "", "ask_options": [],
                        "steps": [{"text": "шаг", "who": "", "deadline": "", "ref": "w1", "quote": text[:40]}]}, {}
            return super().chat_json(system, user, schema, max_tokens)

    def test_deep_pass_adds_referenced_and_neighbor_clauses(self):
        from app.pipeline import build_index
        w84 = win("dbr", "8.4", "Работы без договора запрещены, кроме случая по п. 8.5 настоящего регламента.", 0.9)
        w85 = win("dbr", "8.5", "Без гарантийного письма АМ направляет запрос на старт.", 0.1)
        idx = build_index([w84, w85])
        llm = self.DeepLLM({"ДБРЦТ": "answer"})
        t = run_turn("ДБРЦТ: можно стартовать без договора?", REG, search_for([w84]), llm, session={}, index=idx)
        a = t.answers[0]
        self.assertEqual(a["meta"]["deep"]["added"] >= 1, True)
        self.assertTrue(a["meta"]["deep"]["better"])
        self.assertEqual(a["result"]["verdict"], "answer")
        self.assertTrue(any("копнул глубже" in x for x in t.trace))


class ExternalRefInBaseTest(unittest.TestCase):
    def test_ref_to_document_in_base_is_not_external(self):
        reg = REG + [{"doc_id": "sub", "department": "DBRCT", "stage": "full_cycle", "department_name": "ДБРЦТ",
                      "display_title": "Регламент подбора субподрядчика", "title": "Регламент подбора субподрядчика"}]
        DEPT["sub"] = "DBRCT"
        t = run_turn("ДБРЦТ: за сколько подавать заявку?", reg, search_for(WINS_EXT), FakeLLM({"ДБРЦТ": "answer"}), session={})
        self.assertEqual(t.external_refs, [])            # документ «подбора субподрядчика» уже в базе — не «нет в базе»
        self.assertEqual(t.outcome, "answer")


class ConflictRegistryTest(unittest.TestCase):
    """Поток: анализатор → кандидат → решение СК → ассистент применяет только подтверждённые, актуальные и по теме."""
    def setUp(self):
        self.ws = WINS + [win("dbr", "1.6", "Заявку подают за пять рабочих дней.", 0.3)]

    def conf(self, reg, ws=None):
        from app.pipeline import load_conflicts
        log = []
        return load_conflicts(ws or self.ws, reg.confirmed(), log), log

    def test_candidate_not_applied_until_confirmed(self):
        reg, pid = _registry_with_pair(self.ws, confirm=False)
        self.assertEqual(reg.pairs()[0]["status"], "candidate")
        self.assertEqual(self.conf(reg)[0], {})
        reg.decide(pid, "confirm", "СК")
        self.assertTrue(self.conf(reg)[0])
        row = reg.pairs("confirmed")[0]
        self.assertEqual(row["decided_by"], "СК")
        self.assertTrue(row["decided_at"])

    def test_rejected_not_applied_and_not_resurrected_by_next_night(self):
        reg, pid = _registry_with_pair(self.ws, confirm=False)
        reg.decide(pid, "reject", "СК", "это последовательные шаги, не противоречие")
        rep = {"findings": [{"kind": "contradiction", "a": {"doc_id": "dbr", "clause": "1.6", "quote": "x"},
                             "b": {"doc_id": "dbr", "clause": "1.5", "quote": "y"}}]}
        self.assertEqual(reg.import_report(rep, self.ws)["added"], 0)     # та же пара в другом порядке — не дубль
        self.assertEqual([r["status"] for r in reg.pairs()], ["rejected"])
        self.assertEqual(self.conf(reg)[0], {})

    def test_dept_difference_not_a_candidate(self):
        from app.analysis.registry import ConflictRegistry
        reg = ConflictRegistry(":memory:")
        rep = {"findings": [{"kind": "dept_difference", "a": {"doc_id": "dbr", "clause": "1.5", "quote": "x"},
                             "b": {"doc_id": "dbr", "clause": "1.6", "quote": "y"}}]}
        self.assertEqual(reg.import_report(rep, self.ws)["added"], 0)

    def test_changed_text_not_applied_and_new_candidate(self):
        reg, _ = _registry_with_pair(self.ws)
        ws2 = WINS + [win("dbr", "1.6", "Заявку подают за десять рабочих дней.", 0.3)]
        c, log = self.conf(reg, ws2)
        self.assertEqual(c, {})
        self.assertTrue(log)
        self.assertFalse(reg.pairs(windows=ws2)[0]["current"])
        rep = {"findings": [{"kind": "contradiction", "a": {"doc_id": "dbr", "clause": "1.5", "quote": "x"},
                             "b": {"doc_id": "dbr", "clause": "1.6", "quote": "y"}}]}
        self.assertEqual(reg.import_report(rep, ws2)["added"], 1)        # новая редакция — снова на подтверждение

    def test_manual_pairs_are_candidates(self):
        from app.analysis.registry import ConflictRegistry
        reg = ConflictRegistry(":memory:")
        reg.import_manual([{"department": "DBRCT", "a": "1.5", "b": "1.6", "why": "разные сроки"}], self.ws, "разбор ответов")
        self.assertEqual([r["status"] for r in reg.pairs()], ["candidate"])
        self.assertEqual(self.conf(reg)[0], {})

    def test_off_topic_not_applied(self):
        reg, _ = _registry_with_pair(self.ws)
        c, _ = self.conf(reg)
        t = run_turn("ДБРЦТ: кто проводит установочное совещание с командой?", REG, search_for(WINS), FakeLLM({"ДБРЦТ": "answer"}),
                     session={}, conflicts=c)
        self.assertNotEqual(t.outcome, "conflict")

    def test_on_topic_applied_with_pair_id(self):
        reg, _ = _registry_with_pair(self.ws)
        c, _ = self.conf(reg)
        t = run_turn("ДБРЦТ: за сколько подавать заявку?", REG, search_for(WINS), FakeLLM({"ДБРЦТ": "answer"}), session={}, conflicts=c)
        self.assertEqual(t.outcome, "conflict")
        self.assertIn("DBRCT:1.5↔1.6", t.route["why"])


class PracticePriorityTest(unittest.TestCase):
    """Заказчик 27.11: регламент главнее карточки СК — подтверждённое противоречие пунктов срабатывает,
    даже если в ответе есть практика СК."""
    def test_regulation_conflict_not_hidden_by_practice(self):
        from app.pipeline import apply_conflicts
        conf = {("dbr", "1.5"): [{"clause": "1.6", "why": "разные сроки", "text": "т", "pair": "DBRCT:1.5↔1.6"}]}
        res = {"verdict": "answer", "steps": [{"doc_id": "dbr", "clause": "1.5", "doc_title": "Регламент ДБРЦТ"},
                                              {"doc_id": "", "clause": "P1", "doc_title": "Практика службы качества"}]}
        out = apply_conflicts([{"label": "ДБРЦТ", "result": res}], conf, [], "за сколько подавать заявку сроки разные")
        self.assertEqual(out[0]["result"]["verdict"], "conflict")
        res2 = {"verdict": "answer", "steps": [{"doc_id": "dbr", "clause": "1.5", "doc_title": "Регламент ДБРЦТ"}]}
        out = apply_conflicts([{"label": "ДБРЦТ", "result": res2}], conf, [], "за сколько подавать заявку сроки разные")
        self.assertEqual(out[0]["result"]["verdict"], "conflict")


class DispatcherTest(unittest.TestCase):
    """Г9: диспетчер решения — «по дирекциям раздельно» без вопроса; одно уточнение на вопрос."""
    class DLLM(FakeLLM):
        def __init__(self, decision, *a, **kw):
            super().__init__(*a, **kw)
            self.decision = decision

        def chat_json(self, system, user, schema, max_tokens=1500, **kw):
            if "Ты решаешь, как ответить" in system:
                return {"decision": self.decision, "reason": "тест"}, {}
            return super().chat_json(system, user, schema, max_tokens)

    def run_on(self, decision):
        import app.pipeline as pl
        pl.ON.add("G9")
        try:
            return run_turn("За сколько подавать заявку на расчёт?", REG, search_for(WINS),
                            self.DLLM(decision, {"Техническая дирекция": "answer", "ДБРЦТ": "answer"}), session={})
        finally:
            pl.ON.discard("G9")

    def test_per_department_no_question(self):
        t = self.run_on("per_department")
        self.assertNotEqual(t.outcome, "clarify")
        self.assertEqual(len(t.answers), 2)

    def test_clarify_department(self):
        self.assertEqual(self.run_on("clarify_department").outcome, "clarify")

    def test_same(self):
        t = self.run_on("same")
        self.assertIn("ниже по каждой", t.text)


class OnePracticeTest(unittest.TestCase):
    def test_same_practice_collapses(self):
        from app.pipeline import _one_practice
        st = [{"clause": "P1", "doc_title": "Практика службы качества"}]
        rs = [{"label": d, "result": {"verdict": "answer", "steps": st}} for d in ("ТД", "ДБРЦТ", "ИБ")]
        tr = []
        out = _one_practice(rs, tr)
        self.assertEqual(len(out), 1)
        self.assertIn("Все дирекции", out[0]["label"])

    def test_regulation_steps_kept(self):
        from app.pipeline import _one_practice
        rs = [{"label": "ТД", "result": {"verdict": "answer", "steps": [{"clause": "8.4", "doc_title": "Регламент"}]}},
              {"label": "ИБ", "result": {"verdict": "answer", "steps": [{"clause": "P1", "doc_title": "Практика службы качества"}]}}]
        self.assertIs(_one_practice(rs, []), rs)
