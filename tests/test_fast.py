"""Быстрый ответ (27.11): готовый ход на точный повтор вопроса, прогрев по журналу, ход работы для веба."""
import json
import tempfile
import unittest
from pathlib import Path

from app.answer.cache import AnswerCache
from app.api import server
from app.api.server import App
from app.pipeline import run_turn
from tests.test_pipeline_flow import REG, WINS, FakeLLM, search_for

Q = "За сколько запускать заявку на расчёт?"


def make_app(llm, wins=WINS):
    """App без запуска стенда: только то, что нужно ходу диалога."""
    a = App.__new__(App)
    a.data = Path(tempfile.mkdtemp())
    a.log = a.data / "journal.jsonl"
    a.registry, a.windows, a.llm = list(REG), list(wins), llm
    a.answer_cache, a.live = AnswerCache(), {}
    a.conflicts, a.index, a.abbr, a._abbr_n = [], {}, set(), len(wins)
    a.search = search_for(wins)
    return a


def sess():
    return {"docs": [], "clarifications": 0, "history": [], "pending": None}


class TurnCacheTest(unittest.TestCase):
    def test_repeat_is_served_without_model(self):
        llm = FakeLLM({"ДБРЦТ": "answer"})
        a = make_app(llm)
        t1, c1 = a._turn(Q, sess(), True)
        n = len(llm.calls)
        t2, c2 = a._turn("  За сколько   запускать заявку на расчёт? ", sess(), True)     # другой сотрудник, те же слова
        self.assertEqual((c1, c2), (False, True))
        self.assertEqual(len(llm.calls), n)
        self.assertEqual(t2.outcome, t1.outcome)
        self.assertIs(t2.answers, t1.answers)                    # тот же проверенный ответ, не пересказ
        self.assertEqual(t2.tokens["calls"], 0)
        self.assertEqual(t2.trace[0], App.FAST_NOTE)
        self.assertNotIn(App.FAST_NOTE, t1.trace)                # сохранённый ход не испорчен пометкой

    def test_other_wording_mode_or_base_is_not_served(self):
        llm = FakeLLM({"ДБРЦТ": "answer"})
        a = make_app(llm)
        a._turn(Q, sess(), True)
        self.assertFalse(a._turn(Q.replace("запускать", "подавать"), sess(), True)[1])     # только точный повтор
        self.assertFalse(a._turn(Q, {**sess(), "no_clarify": True}, True)[1])               # другой режим диалога
        self.assertFalse(a._turn(Q, sess(), False)[1])                                      # выбор / продолжение — не новый вопрос
        self.assertTrue(a._turn(Q, sess(), True)[1])
        a.answer_cache.invalidate()                                                         # загрузка документа, карточка СК, сброс демо
        self.assertFalse(a._turn(Q, sess(), True)[1])
        (a.data / "settings.json").write_text("{}")                                         # СК изменила настройки
        self.assertFalse(a._turn(Q, sess(), True)[1])

    def test_clarify_repeat_keeps_second_turn(self):
        llm = FakeLLM({"Техническая дирекция": "answer", "ДБРЦТ": "answer"})
        a = make_app(llm)
        self.assertEqual(a._turn(Q, sess(), True)[0].outcome, "clarify")
        n, s = len(llm.calls), sess()
        t, cached = a._turn(Q, s, True)
        self.assertTrue(cached)
        self.assertEqual(t.outcome, "clarify")
        s.update(docs=["dbr"], clarifications=1)                 # сотрудник выбрал дирекцию
        t2, cached2 = a._turn(Q, s, False)
        self.assertFalse(cached2)
        self.assertEqual([x["label"] for x in t2.answers], ["ДБРЦТ"])
        self.assertEqual(len(llm.calls), n)

    def test_mail_to_quality_is_never_served_from_cache(self):
        """Письмо в СК — действие, а не ответ: каждый раз заново (иначе обращение могло бы «ответиться» старым ходом)."""
        a = make_app(FakeLLM({}))
        q = "Сколько лет хранить документацию проекта?"
        self.assertEqual(a._turn(q, sess(), True)[0].outcome, "escalate")
        t, cached = a._turn(q, sess(), True)
        self.assertEqual((t.outcome, cached), ("escalate", False))

    def test_switch_off(self):
        a = make_app(FakeLLM({"ДБРЦТ": "answer"}))
        server.OFF.add("FAST")
        try:
            a._turn(Q, sess(), True)
            self.assertFalse(a._turn(Q, sess(), True)[1])
        finally:
            server.OFF.discard("FAST")

    def test_warm_from_journal(self):
        llm = FakeLLM({"ДБРЦТ": "answer"})
        a = make_app(llm)
        rows = [{"q": Q, "outcome": "answer"}, {"q": Q, "outcome": "answer"}, {"q": "Письмо", "outcome": "escalate"}, {"feedback": "up"}]
        a.log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
        self.assertEqual(a.warm(), 1)                            # один раз, без обращений в СК
        n = len(llm.calls)
        self.assertTrue(a._turn(Q, sess(), True)[1])
        self.assertEqual(len(llm.calls), n)


class LiveProgressTest(unittest.TestCase):
    def test_steps_reported_and_not_in_answer(self):
        s = {"_live": []}
        steps = s["_live"]
        t = run_turn(Q, REG, search_for(WINS), FakeLLM({"Техническая дирекция": "answer", "ДБРЦТ": "answer"}), session=s)
        self.assertGreaterEqual(len(steps), 3)
        self.assertTrue(any("сверяю цитаты" in x for x in steps))
        self.assertFalse(set(steps) & set(t.trace))              # показ ожидания — не часть трассы и не часть ответа
        self.assertNotIn("_live", s)
        self.assertEqual(run_turn(Q, REG, search_for(WINS), FakeLLM({"ДБРЦТ": "answer"}), session={}).outcome, "answer")   # без показа ожидания ход тот же
