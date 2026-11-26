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


class SynonymTest(unittest.TestCase):
    def test_colloquial_synonym_of_term_in_clause_kept(self):
        w = [{**W[0], "clause": "13.1", "text": "13.1. РП заводит бюджет проекта в учетной системе в течение 2 рабочих дней."}]
        raw = {"verdict": "answer", "summary": "", "caveat": "", "missing": "", "steps": [
            {"text": "Проджект менеджер (РП) заводит бюджет проекта в течение 2 рабочих дней.", "who": "РП", "deadline": "",
             "ref": "w1", "quote": "РП заводит бюджет проекта в учетной системе"}]}
        self.assertEqual(check(raw, w, "В какой срок проджект менеджер заводит бюджет проекта?")["verdict"], "answer")

    def test_summary_number_not_in_source_replaced(self):
        raw = {"verdict": "answer", "summary": "Не менее чем за 5 рабочих дней.", "caveat": "", "missing": "", "steps": [
            {"text": "Инициируйте заявку не менее чем за 3 рабочих дня", "who": "", "deadline": "", "ref": "w1",
             "quote": "не менее чем за 3 рабочих дня до подачи предложения"}]}
        out = check(raw, W, "За сколько подавать заявку?")
        self.assertNotIn("5", out["summary"])


class PermitBanTest(unittest.TestCase):
    """Судья: q145 «Да, можете быть ГИП…» при запрете в пункте → оговорка с цитатой запрета; исключение «за исключением RFI» — не запрет."""
    W = [{**W[0], "clause": "5.1.3.3", "text": "5.1.3.3. Если сотрудник является единственным исполнителем по одной из тем проекта, то он не может быть назначен ГИП."}]

    def raw(self, summary):
        return {"verdict": "answer", "summary": summary, "caveat": "", "missing": "", "steps": [
            {"text": "ГИП назначает руководитель", "who": "", "deadline": "", "ref": "w1",
             "quote": "является единственным исполнителем по одной из тем проекта"}]}

    def test_permit_vs_ban_downgraded(self):
        out = check(self.raw("Да, вы можете быть ГИП по другой теме, если не являетесь единственным исполнителем по ней."), self.W, "Могу ли я быть ГИП?")
        self.assertEqual(out["verdict"], "answer_with_caveat")
        self.assertIn("не может быть назначен ГИП", out["summary"])
        self.assertNotIn("Да, вы можете", out["summary"])

    def test_negative_answer_not_touched(self):
        out = check(self.raw("Нет, вы не можете быть ГИП по теме, где вы единственный исполнитель."), self.W, "Могу ли я быть ГИП?")
        self.assertEqual(out["verdict"], "answer")

    def test_exception_named_not_ban(self):
        w = [{**W[0], "clause": "2.4", "text": "2.4. Бюджетная оценка не может использоваться для подачи в конкурс (за исключением RFI)."}]
        raw = {"verdict": "answer", "summary": "Да, бюджетная оценка может использоваться для подачи в RFI.", "caveat": "", "missing": "",
               "steps": [{"text": "Для RFI можно", "who": "", "deadline": "", "ref": "w1", "quote": "Бюджетная оценка не может использоваться для подачи в конкурс"}]}
        self.assertEqual(check(raw, w, "Можно ли бюджетную оценку в RFI?")["verdict"], "answer")


class DocAbbrTest(unittest.TestCase):
    """Г12: расшифровки сокращений — из текста документов, по первым буквам; глоссарий организаторов не используется."""
    def test_extract(self):
        from app.answer.core import extract_abbr
        ws = [{"text": "Ожидает ответа от SS (Менеджера по продаже решений). Назначение руководителя проекта (РП) — в течение дня."},
              {"text": "SS (Менеджер по продаже решений) направляет КП. Если при запросе расчёта (РТЗ) нет данных."}]
        a = extract_abbr(ws)
        self.assertIn("продаже решений", a["SS"])
        self.assertEqual(a["РП"], "Руководителя проекта")
        self.assertNotIn("РТЗ", a)


class FiredPairsTest(unittest.TestCase):
    def test_colloquial_pairs(self):
        from app.answer.core import fired_pairs
        self.assertTrue(any("офер" in x for x in fired_pairs("Кто относит офер клиенту?")))
        self.assertEqual(fired_pairs("Кто назначает руководителя проекта?"), [])
