import unittest

from app.pipeline import doc_abbr, keep_doc_abbr

WIN = [{"text": "TPS обязан не выполнять финализированный расчет ТЗТ."},
       {"text": "РП ведёт проект; ФТТ согласует заказчик; расчет готовит TPS."}]


class AbbrTest(unittest.TestCase):
    def test_doc_abbr_excludes_glossary(self):
        ab = doc_abbr(WIN)
        self.assertIn("ТЗТ", ab)
        self.assertNotIn("РП", ab)          # есть в словаре — модель раскрывает сама
        self.assertNotIn("ФТТ", ab)

    def test_keeps_abbreviation_from_question(self):
        tr = []
        u = keep_doc_abbr("кто делет тзт", {"queries": ["кто разрабатывает функционально-технические требования", "роли в ФТТ"]},
                          doc_abbr(WIN), tr)
        self.assertEqual(u["queries"], ["кто делет ТЗТ"])
        self.assertTrue(tr)

    def test_untouched_without_abbreviation(self):
        u = {"queries": ["кто готовит расчёт"]}
        self.assertIs(keep_doc_abbr("кто делает расчёт", u, doc_abbr(WIN), []), u)


if __name__ == "__main__":
    unittest.main()


class DeptHintTest(unittest.TestCase):
    def test_hint(self):
        from app.pipeline import DEPT_HINT
        self.assertIsNone(DEPT_HINT.search("Что предпринимает команда проекта, если не хватает исполнителей?"))
        for q in ("У безопасников можно начать?", "In the technical department, who closes?", "А в ИБ как?", "Техдирекция: кто?"):
            self.assertIsNotNone(DEPT_HINT.search(q), q)


class LooseQuoteTest(unittest.TestCase):
    def test_list_marker_quote_accepted(self):
        from app.answer.core import check
        w = [{"window_id": "a", "doc_id": "d", "department": "TECH", "department_name": "ТД", "doc_title": "t", "section_title": "s",
              "path": "p", "kind": "clause", "clause": "2.5",
              "text": "2.5. Для привлечения TPS SS обязан:\n• приложить квалификационную анкету;\n• приложить ТЗ заказчика."}]
        r = check({"verdict": "answer", "summary": "Нужно приложить квалификационную анкету",
                   "steps": [{"text": "Приложить квалификационную анкету", "who": "SS", "ref": "w1",
                              "quote": "SS обязан приложить квалификационную анкету"}]}, w)
        self.assertEqual(len(r["steps"]), 1)
        r = check({"verdict": "answer", "summary": "x", "steps": [{"text": "x", "ref": "w1", "quote": "SS обязан подписать договор"}]}, w)
        self.assertEqual(len(r["steps"]), 0)


class ElidedQuoteTest(unittest.TestCase):
    def test_elided(self):
        from app.answer.core import _elided_ok
        t = "Архитектор фиксирует новые сроки выдачи расчёта в заявке и уведомляет SS. Если сроки не устраивают, Архитектор закрывает заявку."
        self.assertTrue(_elided_ok("Архитектор фиксирует новые сроки выдачи... Архитектор закрывает заявку", t))
        self.assertFalse(_elided_ok("Архитектор закрывает заявку... Архитектор фиксирует новые сроки", t))   # порядок
        self.assertFalse(_elided_ok("Архитектор фиксирует новые сроки... SS подписывает договор", t))

    def test_elided_gap_and_word_boundary(self):
        from app.answer.core import _elided_ok
        far = "РП вправе начать работы после подписания договора. " + "Прочие условия проекта описаны отдельно. " * 12 + \
              "Запрещено привлекать подрядчика без согласования сметы с директором."
        # два куска из дальних фраз одного окна складываются в правило, которого в пункте нет
        self.assertFalse(_elided_ok("РП вправе начать работы... без согласования сметы с директором", far))
        self.assertTrue(_elided_ok("РП вправе начать работы... после подписания договора", far))
        self.assertFalse(_elided_ok("в течение 5 дней... после подписания договора", "Срок: в течение 15 дней с даты и после подписания договора."))

    def test_loose_and_elided_quotes_still_checked(self):
        """Цитата без маркеров списка или с «...» — не повод пропускать проверки чисел и ролей (Fable 27.11, Г17-ревью)."""
        from app.answer.core import check
        w = [{"window_id": "a", "doc_id": "d", "department": "TECH", "department_name": "ТД", "doc_title": "t", "section_title": "s",
              "path": "p", "kind": "clause", "clause": "2.5",
              "text": "2.5. Для привлечения TPS SS обязан:\n• приложить квалификационную анкету;\n• приложить ТЗ заказчика. "
                      "Заявка рассматривается в течение 2 рабочих дней."}]
        for quote in ("SS обязан приложить квалификационную анкету",                                    # без маркера списка
                      "SS обязан: • приложить квалификационную анкету... Заявка рассматривается в течение 2 рабочих дней"):
            r = check({"verdict": "answer", "summary": "Нужно приложить квалификационную анкету",
                       "steps": [{"text": "Приложить квалификационную анкету в течение 5 дней", "who": "SS", "deadline": "5 дней",
                                  "ref": "w1", "quote": quote}]}, w)
            self.assertEqual(len(r["steps"]), 1, quote)
            self.assertEqual(r["steps"][0].get("replaced"), "number_not_in_source", quote)       # выдуманный срок не показан
            self.assertNotIn("5 дней", r["steps"][0]["text"] + r["steps"][0]["deadline"], quote)
