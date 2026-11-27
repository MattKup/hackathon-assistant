"""Г19/Г20 (разбор лишних уточнений дирекции 27.11): выключатели меняют только то, что задумано."""
import unittest
import app.pipeline as p


class Cap:
    def __init__(self, same):
        self.same, self.calls = same, []

    def chat_json(self, system, user, schema, max_tokens=0):
        self.calls.append((system, user, schema))
        return {"same": self.same, "difference": "разница"}, {}


def ans(label, verdict="answer"):
    return {"label": label, "doc_id": label, "result": {"verdict": verdict, "summary": "s", "steps": [], "caveat": ""}, "meta": {}}


class G20Test(unittest.TestCase):
    def tearDown(self):
        p.ON.discard("G20")

    def test_default_prompt(self):
        llm = Cap(False)
        p.answers_same(llm, "q", [ans("ТД"), ans("ИБ", "answer_with_caveat")])
        self.assertIs(llm.calls[0][0], p.SAME_SYSTEM)
        self.assertNotIn("(ответ с оговоркой)", llm.calls[0][1])

    def test_g20_prompt_difference_first_and_verdicts(self):
        p.ON.add("G20")
        llm = Cap(True)
        same, _ = p.answers_same(llm, "q", [ans("ТД"), ans("ИБ", "answer_with_caveat")])
        sysmsg, user, schema = llm.calls[0]
        self.assertIs(sysmsg, p.SAME_SYSTEM2)
        self.assertEqual(schema["required"], ["difference", "same"])
        self.assertIn("ИБ (ответ с оговоркой)", user)
        self.assertTrue(same)


if __name__ == "__main__":
    unittest.main()
