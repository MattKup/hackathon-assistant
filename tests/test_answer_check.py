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

    def test_step_added_from_question_rejected(self):
        w = [{**W[0], "clause": "8.3", "text": "8.3. Приступать к выполнению работ без подписанного договора запрещается."}]
        q = "Техническая дирекция: можно ли начать работы по гарантийному письму заказчика до подписания договора?"
        bad = {"verdict": "answer_with_caveat", "summary": "", "caveat": "", "missing": "", "steps": [
            {"text": "Убедитесь, что договор не подписан, и проверьте наличие гарантийного письма.", "who": "", "deadline": "",
             "ref": "w1", "quote": "Приступать к выполнению работ без подписанного договора запрещается"}]}
        self.assertEqual(check(bad, w, q)["rejected"][0]["reason"], "added_from_question")
        good = {**bad, "steps": [{**bad["steps"][0], "text": "Без подписанного договора приступать к работам запрещено."}]}
        self.assertEqual(len(check(good, w, q)["steps"]), 1)

    def test_user_prompt_has_clause(self):
        self.assertIn("п. 1.5", build_user("q", W, "ДБРЦТ"))


if __name__ == "__main__":
    unittest.main()


class EntityCheckTest(unittest.TestCase):
    """Судья v7: додуманные роли (q012, q118) и ТКП вместо КП (q129); иероглифы в сроке (q026)."""

    def setUp(self):
        self.w = [{**W[0], "clause": "6.4", "text": "6.4. На основе итогового расчёта трудозатрат SS готовит КП и направляет его Заказчику и закрывает Заявку."}]

    def step(self, text, who="", deadline=""):
        return {"verdict": "answer", "summary": "", "caveat": "", "missing": "", "steps": [
            {"text": text, "who": who, "deadline": deadline, "ref": "w1", "quote": "SS готовит КП и направляет его Заказчику"}]}

    def test_role_not_in_source_rejected(self):
        r = check(self.step("Менеджер по качеству готовит КП и направляет его Заказчику."), self.w, "Кто отправляет КП?")
        self.assertEqual(r["rejected"][0]["reason"], "entity_not_in_source")

    def test_abbr_not_in_source_rejected(self):
        r = check(self.step("SS готовит ТКП и направляет его Заказчику."), self.w, "Кто отправляет предложение?")
        self.assertEqual(r["rejected"][0]["reason"], "entity_not_in_source")

    def test_expansion_counts_as_present(self):
        w = [{**self.w[0], "text": "9.4. Основными задачами РП являются мониторинг и управление рисками."}]
        st = {"verdict": "answer", "summary": "", "caveat": "", "missing": "", "steps": [
            {"text": "Руководитель проекта управляет рисками.", "who": "", "deadline": "", "ref": "w1", "quote": "Основными задачами РП являются мониторинг"}]}
        self.assertEqual(len(check(st, w, "Кто управляет рисками?")["steps"]), 1)

    def test_role_from_question_allowed(self):
        r = check(self.step("Вы как Архитектор: SS готовит КП и направляет его Заказчику."), self.w, "Я Архитектор, кто отправляет КП?")
        self.assertEqual(len(r["steps"]), 1)

    def test_who_not_in_source_blanked(self):
        r = check(self.step("SS готовит КП и направляет его Заказчику.", who="Логист"), self.w, "Кто отправляет КП?")
        self.assertEqual(r["steps"][0]["who"], "")

    def test_garbled_rejected(self):
        r = check(self.step("SS готовит КП и направляет его Заказчику.", deadline="не позднее手続きы"), self.w, "Когда?")
        self.assertEqual(r["rejected"][0]["reason"], "garbled")
