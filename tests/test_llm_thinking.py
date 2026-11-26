import io, json, unittest
from unittest import mock
from app.answer import llm


def _resp(content, finish="stop"):
    body = {"choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    r = io.BytesIO(json.dumps(body).encode())
    r.__enter__ = lambda *a: r
    r.__exit__ = lambda *a: False
    return r


class ThinkingFallback(unittest.TestCase):
    def test_empty_thinking_answer_falls_back_to_plain(self):
        calls = []

        def fake(req, timeout):
            b = json.loads(req.data)
            calls.append(b["chat_template_kwargs"]["enable_thinking"])
            return _resp("<think>долго</think>", "length") if b["chat_template_kwargs"]["enable_thinking"] else _resp('{"ok": 1}')
        c = llm.LLM.__new__(llm.LLM)
        c.url, c.model, c.timeout = "http://x", "m", 1
        with mock.patch("urllib.request.urlopen", fake):
            out, meta = c.chat_json("s", "u", {}, thinking=True)
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(calls, [True, False])
        self.assertTrue(meta["thinking_fallback"])
        self.assertEqual(meta["thinking_finish"], "length")


if __name__ == "__main__":
    unittest.main()
