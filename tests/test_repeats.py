"""Мониторинг повторов: «вопрос передавали N раз за период» → один сигнал СК за период, письма не склеиваются."""
import sqlite3
import unittest

from app.escalation.repeats import Repeats, signal_email, words_similar

CONF = {"period_days": 7, "threshold": 3, "similarity": 0.8, "similarity_words": 0.6}
Q1 = "Сколько лет хранить проектную документацию после перевода проекта в архив?"
Q2 = "Сколько хранить проектную документацию в архиве после закрытия проекта?"
Q3 = "Кто согласует командировку сотрудника проекта?"


class RepeatsTest(unittest.TestCase):
    def setUp(self):
        self.r = Repeats(sqlite3.connect(":memory:"), words_similar(CONF["similarity_words"]), CONF)

    def test_signal_once_at_threshold_with_paraphrases(self):
        t = 1_000_000
        self.assertIsNone(self.r.record(Q1, "a@x", "C1", "нет", t)["signal"])
        self.assertIsNone(self.r.record(Q3, "b@x", "C2", "нет", t + 1)["signal"])      # другой вопрос не считается
        self.assertEqual(self.r.record(Q2, "c@x", "C3", "нет", t + 2)["count"], 2)
        r = self.r.record(Q1, "d@x", "C4", "нет", t + 3)
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["signal"]["case_ids"], ["C1", "C3", "C4"])
        self.assertIsNone(self.r.record(Q2, "e@x", "C5", "нет", t + 4)["signal"])     # за период — один сигнал
        self.assertIn("3 раз за 7 дн.", signal_email(r["signal"], "qa@x")["Subject"])

    def test_period_window(self):
        t, day = 1_000_000, 86400
        self.r.record(Q1, "a@x", "C1", "нет", t)
        self.r.record(Q1, "b@x", "C2", "нет", t + 5 * day)
        self.assertEqual(self.r.record(Q1, "c@x", "C3", "нет", t + 9 * day)["count"], 2)   # первый — вне периода

    def test_groups_for_analytics(self):
        t = 1_000_000
        for i, q in enumerate([Q1, Q2, Q3, Q1]):
            self.r.record(q, f"{i}@x", f"C{i}", "нет", t + i)
        g = self.r.groups(now=t + 10)
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["count"], 3)
        self.assertTrue(g[0]["signal"])
        self.assertIn(Q2, g[0]["variants"])


if __name__ == "__main__":
    unittest.main()
