import unittest
from app.pipeline import build_index, related_clauses


def w(c, t, doc="D"):
    return {"window_id": f"{doc}-{c}", "doc_id": doc, "clause": c, "text": t, "doc_title": "Регламент"}


WS = [w("8.3", "Работы без подписанного договора и согласованной сметы запрещены."),
      w("8.4", "Исключение – начало работ без подписанного договора и согласованной сметы при наличии гарантийного письма заказчика."),
      w("8.5", "Отчёт о работе команды направляется ежемесячно."),
      w("9.1", "Смета утверждается директором.")]


class Related(unittest.TestCase):
    def setUp(self):
        self.idx = build_index(WS)

    def res(self, clause):
        return {"verdict": "answer", "summary": "Без договора работы начинать нельзя.",
                "steps": [{"clause": clause, "window_id": f"D-{clause}"}]}

    def test_exception_neighbour_found(self):
        rel = related_clauses("Можно ли начать работы без подписанного договора?", self.res("8.3"), WS[:1], self.idx)
        self.assertEqual([x["clause"] for x in rel], ["8.4"])
        self.assertIn("гарантийного письма", rel[0]["quote"])

    def test_off_topic_neighbour_skipped(self):
        rel = related_clauses("Как часто отправлять отчёт команды?", self.res("8.5"), [WS[2]], self.idx)
        self.assertEqual(rel, [])

    def test_no_answer_no_hint(self):
        r = {"verdict": "no_answer", "summary": "", "steps": []}
        self.assertEqual(related_clauses("договор", r, WS, self.idx), [])


if __name__ == "__main__":
    unittest.main()
