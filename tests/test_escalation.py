import json
import tempfile
import unittest
from pathlib import Path

from app.escalation.mail import case_email, strip_quoted
from app.escalation.process import practice_windows, process_replies
from app.escalation.store import Store


class FakeMailbox:
    def __init__(self, replies):
        self._r = replies

    def replies(self):
        return self._r


class FakeLLM:
    def __init__(self, res):
        self.res = res

    def chat_json(self, *a, **k):
        return self.res, {}


class EscalationTest(unittest.TestCase):
    def setUp(self):
        self.s = Store(str(Path(tempfile.mkdtemp()) / "t.sqlite"))
        self.c = self.s.new_case("ivanov@example.test", "Можно ли стартовать по гарантийному письму в Тех дирекции?",
                                 ["TECH"], [], [{"doc": "Тех", "clause": "8.4", "text": "При необходимости старта…"}], "нет пункта")

    def test_email_has_thread_ids_and_cc(self):
        m = case_email(self.c, "qa@example.test")
        self.assertEqual(m["To"], "qa@example.test")
        self.assertEqual(m["Cc"], "ivanov@example.test")
        self.assertEqual(m["X-Case-Id"], self.c["case_id"])
        self.assertIn("гарантийному письму", m.get_content())

    def test_interim_not_published(self):
        mb = FakeMailbox([{"id": "m1", "case_msgid": self.c["message_id"], "sender": "qa@example.test", "text": "Уточняю у коллег"}])
        ev = process_replies(self.s, mb, FakeLLM({"is_final": False, "answer": "", "applies_to": [], "outcome": "practice"}))
        self.assertEqual(ev[0]["event"], "interim")
        self.assertEqual(self.s.practices(), [])
        self.assertEqual(self.s.case(self.c["case_id"])["status"], "in_work")

    def test_final_published_for_all_and_searchable(self):
        mb = FakeMailbox([{"id": "m2", "case_msgid": self.c["message_id"], "sender": "qa@example.test",
                           "text": "Гарантийное письмо — основание для старта во всех дирекциях."}])
        ev = process_replies(self.s, mb, FakeLLM({"is_final": True, "answer": "Можно по гарантийному письму во всех дирекциях.",
                                                   "question_general": "Можно ли стартовать по гарантийному письму?",
                                                   "variants": ["Старт до договора по гарантийке?"],
                                                   "applies_to": ["ALL"], "outcome": "practice"}))
        self.assertEqual(ev[0]["event"], "draft")
        self.assertEqual(practice_windows(self.s), [])          # без подтверждения в поиск не попадает
        self.s.approve(ev[0]["practice_id"], "эксперт")
        ws = practice_windows(self.s)
        self.assertEqual({w["department"] for w in ws}, {"TECH", "IB", "DBRCT"})
        # повторная обработка того же письма ничего не делает
        self.assertEqual(process_replies(self.s, mb, FakeLLM({})), [])

    def test_revoke_hides_practice(self):
        pid = self.s.publish(self.c["case_id"], "qa", "q", "a", ["TECH"], [], "practice")
        self.s.revoke(pid, "qa")
        self.assertEqual(practice_windows(self.s), [])

    def test_strip_quoted(self):
        self.assertEqual(strip_quoted("Ответ.\n\nIvan пишет:\n> старое"), "Ответ.")


if __name__ == "__main__":
    unittest.main()


class DestinationTest(unittest.TestCase):
    def test_default_faq_and_switch_to_regulation(self):
        from app.escalation.store import Store
        s = Store(":memory:")
        c = s.new_case("e@x", "q", ["TECH"], [], [], "r")
        pid = s.publish(c["case_id"], "qa", "q", "a", ["TECH"], [], "practice", status="draft")
        self.assertEqual(s.practices(only_published=False)[0]["destination"], "faq")
        s.approve(pid, "qa", destination="regulation")
        p = s.practices()[0]
        self.assertEqual((p["status"], p["destination"]), ("published", "regulation"))
        with self.assertRaises(ValueError):
            s.set_destination(pid, "x")


class DiscrepancyTest(unittest.TestCase):
    def test_marker_moves_to_report_field(self):
        from app.pipeline import split_discrepancy
        r = split_discrepancy({"verdict": "answer", "missing": "РАСХОЖДЕНИЕ: разъяснение службы качества расходится с п. 8.4"})
        self.assertEqual(r["missing"], "")
        self.assertIn("п. 8.4", r["discrepancy"])
        self.assertNotIn("discrepancy", split_discrepancy({"verdict": "answer", "missing": "нет срока"}))

    def test_store_report(self):
        from app.escalation.store import Store
        s = Store(":memory:")
        s.add_discrepancy("q", "ДБРЦТ", "расходится с п. 8.4", ["P1"])
        self.assertEqual(s.discrepancies()[0]["practice_ids"], ["P1"])


class SettingsTest(unittest.TestCase):
    def test_overrides_apply_and_persist(self):
        import tempfile
        from pathlib import Path
        from app import config
        d = Path(tempfile.mkdtemp()) / "settings.json"
        config.use_overrides(d)
        try:
            out = config.save_overrides({"qa_topics": ["командировки"], "secret": 1})
            self.assertEqual(out["qa_topics"], ["командировки"])
            self.assertNotIn("secret", d.read_text())
            config.use_overrides(d)
            self.assertEqual(config.qa_topics(), ["командировки"])
        finally:
            config._OVR_PATH, config._CFG = None, None
