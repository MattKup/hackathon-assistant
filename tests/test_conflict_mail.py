import json
import unittest

from app.escalation.mail import case_email


def case(reason):
    return {"question": "Можно ли начать работы без договора?", "departments": json.dumps(["ДБРЦТ"]), "fragments": "[]",
            "dialogue": "[]", "reason": reason, "employee": "e@example.test", "message_id": "<m@x>", "case_id": "C1"}


class ConflictMail(unittest.TestCase):
    def test_conflict_asks_for_decision_and_regulation_change(self):
        m = case_email(case("противоречие между пунктами регламента: ДБРЦТ 8.4↔8.5"), "qa@example.test")
        body = m.get_content()
        self.assertTrue(m["Subject"].startswith("Противоречие в регламенте"))
        self.assertIn("изменение регламента", body)
        self.assertIn("индивидуальное согласование", body)

    def test_plain_case_unchanged(self):
        m = case_email(case("в регламентах нет опоры для ответа"), "qa@example.test")
        self.assertTrue(m["Subject"].startswith("Вопрос по регламентам"))
        self.assertNotIn("изменение регламента", m.get_content())


if __name__ == "__main__":
    unittest.main()
