import unittest

from app.dialog.clarify import decide, mentioned
from app.search.bm25 import BM25, stem

REG = [
    {"doc_id": "ib_pre", "department": "IB", "stage": "presale", "department_name": "Дирекция ИБ"},
    {"doc_id": "ib_prj", "department": "IB", "stage": "project", "department_name": "Дирекция ИБ"},
    {"doc_id": "dbr", "department": "DBRCT", "stage": "full_cycle", "department_name": "ДБРЦТ"},
    {"doc_id": "tech", "department": "TECH", "stage": "full_cycle", "department_name": "Техническая дирекция"},
]
WIN = [
    {"window_id": "1", "doc_id": "dbr", "path": "ДБРЦТ › Инициация", "text": "Заявку на расчёт инициировать не менее чем за 3 рабочих дня до предложения заказчику."},
    {"window_id": "2", "doc_id": "tech", "path": "Тех › Расчёт", "text": "Расчёт должен быть инициирован за две недели до предложения заказчику."},
    {"window_id": "3", "doc_id": "ib_prj", "path": "ИБ › Субподряд", "text": "РП отвечает за заключение договоров с субподрядчиками."},
]


class SearchClarifyTest(unittest.TestCase):
    def setUp(self):
        self.bm = BM25(WIN)

    def test_stem_merges_forms(self):
        self.assertEqual(stem("договора"), stem("договоров"))

    def test_bm25_finds(self):
        hits = self.bm.search("кто заключает договор с субподрядчиком")
        self.assertEqual(hits[0]["window_id"], "3")

    def test_clarify_when_two_departments(self):
        hits = self.bm.search("за сколько инициировать расчёт до предложения заказчику")
        d = decide("за сколько инициировать расчёт до предложения заказчику", hits, REG)
        self.assertEqual(d.action, "clarify")
        self.assertEqual({o["department"] for o in d.options}, {"DBRCT", "TECH"})

    def test_named_department_no_clarify(self):
        q = "в ДБР за сколько инициировать расчёт?"
        d = decide(q, self.bm.search(q), REG)
        self.assertEqual((d.action, d.doc_ids), ("answer", ["dbr"]))

    def test_ib_stage(self):
        self.assertEqual(mentioned("пресейл в дирекции ИБ", REG), ["ib_pre"])
        self.assertEqual(mentioned("что делает РП в проекте ИБ", REG), ["ib_prj"])
        self.assertEqual(sorted(mentioned("ИБ", REG)), ["ib_pre", "ib_prj"])

    def test_session_department_used(self):
        q = "а сроки?"
        d = decide(q, self.bm.search("сроки расчёта"), REG, session_docs=["tech"])
        self.assertEqual((d.action, d.doc_ids), ("answer", ["tech"]))

    def test_filter_allowed(self):
        hits = self.bm.search("расчёт предложения заказчику", allowed={"tech"})
        self.assertTrue(all(h["doc_id"] == "tech" for h in hits))


if __name__ == "__main__":
    unittest.main()


class QuestionsIndexTest(unittest.TestCase):
    """Г14: попадание по сгенерированному вопросу возвращает пункт-родитель (текст пункта), а не вопрос."""
    def test_augment_collapse(self):
        from app.search.questions import augment, collapse
        ws = [{"window_id": "w1", "doc_id": "d", "department": "TECH", "stage": "x", "clause": "1.1", "section": "1",
               "section_title": "s", "path": "Док", "text": "Пункт про сроки."}]
        allw = augment(ws, {"w1": ["шеф кивнул — можно?"]})
        self.assertEqual(len(allw), 2)
        hits = [{**allw[1], "score": 0.9}, {**ws[0], "score": 0.4}]
        out = collapse(hits, {"w1": ws[0]}, 5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "Пункт про сроки.")
        self.assertEqual(out[0]["score"], 0.9)
        self.assertEqual(out[0]["via_q"], "шеф кивнул — можно?")
