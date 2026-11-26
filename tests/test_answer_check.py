import unittest

from app.answer.core import build_user, check

W = [{"window_id": "a", "department": "DBRCT", "department_name": "ДБРЦТ", "doc_title": "Проектная деятельность ДБРЦТ",
      "clause": "1.5", "section_title": "Проработка", "text": "1.5. Заявка должна быть инициирована не менее чем за 3 рабочих дня до подачи предложения Заказчику."}]


class CheckTest(unittest.TestCase):
    def test_good_step_kept(self):
        raw = {"verdict": "answer", "summary": "", "caveat": "", "missing": "",
               "steps": [{"text": "Создайте заявку минимум за 3 рабочих дня", "who": "SS", "deadline": "3 рабочих дня",
                          "ref": "w1", "quote": "инициирована не менее чем за 3 рабочих дня"}]}
        out = check(raw, W)
        self.assertEqual(len(out["steps"]), 1)
        self.assertEqual(out["steps"][0]["clause"], "1.5")

    def test_paraphrased_quote_rejected_and_downgraded(self):
        raw = {"verdict": "answer", "summary": "", "caveat": "", "missing": "",
               "steps": [{"text": "x", "who": "", "deadline": "", "ref": "w1", "quote": "заявку нужно создать за три дня"}]}
        out = check(raw, W)
        self.assertEqual(out["rejected"][0]["reason"], "quote_not_verbatim")
        self.assertEqual(out["verdict"], "no_answer")

    def test_wrong_number_rejected(self):
        raw = {"verdict": "answer", "summary": "", "caveat": "", "missing": "",
               "steps": [{"text": "за 5 рабочих дней", "who": "", "deadline": "", "ref": "w1",
                          "quote": "не менее чем за 3 рабочих дня"}]}
        self.assertEqual(check(raw, W)["rejected"][0]["reason"], "number_not_in_source")

    def test_bad_ref(self):
        raw = {"verdict": "answer", "summary": "", "caveat": "", "missing": "",
               "steps": [{"text": "x", "who": "", "deadline": "", "ref": "w9", "quote": "Заявка"}]}
        self.assertEqual(check(raw, W)["rejected"][0]["reason"], "ref_missing")

    def test_quote_normalization(self):
        raw = {"verdict": "answer", "summary": "", "caveat": "", "missing": "",
               "steps": [{"text": "x", "who": "", "deadline": "", "ref": "w1", "quote": "Заявка  должна быть инициирована."}]}
        self.assertEqual(len(check(raw, W)["steps"]), 1)

    def test_near_verbatim_quote_repaired(self):
        raw = {"verdict": "answer", "summary": "", "caveat": "", "missing": "",
               "steps": [{"text": "x", "who": "", "deadline": "", "ref": "w1",
                          "quote": "Заявка должна быть инициирована не менее чем за 3 рабочих дня до предложения Заказчику"}]}
        out = check(raw, W)
        self.assertEqual(len(out["steps"]), 1)
        self.assertTrue(out["steps"][0]["quote_repaired"])

    def test_user_prompt_has_clause(self):
        self.assertIn("п. 1.5", build_user("q", W, "ДБРЦТ"))


if __name__ == "__main__":
    unittest.main()
