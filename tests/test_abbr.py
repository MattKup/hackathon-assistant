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
