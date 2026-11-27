import unittest

from app.answer.core import check_consider


def W(i, clause, text):
    return {"window_id": f"w{i}", "clause": clause, "text": text, "department": "DBRCT", "doc_title": "Регламент", "kind": "clause"}


WS = [W(1, "8.3", "Работы без подписанного договора запрещены."),
      W(2, "8.4", "Исключение – наличие подписанного гарантийного письма и письменное согласование Директора направления."),
      W(3, "10.2", "Смета согласуется в течение 3 рабочих дней.")]


class Consider(unittest.TestCase):
    def base(self):
        return {"verdict": "answer", "steps": [{"clause": "8.3", "doc_title": "Регламент"}], "rejected": []}

    def test_verbatim_item_kept(self):
        raw = {"consider": [{"text": "Исключение: гарантийное письмо и согласование Директора направления.", "ref": "w2",
                             "quote": "наличие подписанного гарантийного письма"}]}
        out = check_consider(self.base(), raw, WS, "можно ли начать работы без договора")
        self.assertEqual([c["clause"] for c in out["consider"]], ["8.4"])

    def test_wrong_quote_and_new_number_rejected(self):
        raw = {"consider": [{"text": "Смету согласуют за 5 дней.", "ref": "w3", "quote": "Смета согласуется в течение 3 рабочих дней"},
                            {"text": "Нужен приказ.", "ref": "w2", "quote": "приказ директора"}]}
        out = check_consider(self.base(), raw, WS, "можно ли начать работы без договора")
        self.assertEqual(out["consider"], [])
        self.assertTrue(all(r["reason"].startswith("consider_") for r in out["rejected"]))

    def test_same_clause_as_answer_dropped(self):
        raw = {"consider": [{"text": "Без договора нельзя.", "ref": "w1", "quote": "Работы без подписанного договора запрещены"}]}
        out = check_consider(self.base(), raw, WS, "можно ли начать работы без договора")
        self.assertEqual(out["consider"], [])

    def test_no_answer_empty(self):
        out = check_consider({"verdict": "no_answer", "steps": [], "rejected": []}, {"consider": [{"text": "x", "ref": "w1", "quote": "Работы"}]}, WS, "q")
        self.assertEqual(out["consider"], [])


if __name__ == "__main__":
    unittest.main()
