"""Клиент OpenAI-совместимого API vLLM (стенд: модель qwen38, без режима размышлений)."""
import json
import time
import urllib.error
import urllib.request


class LLM:
    def __init__(self, base_url, model="qwen38", timeout=90):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model, self.timeout = model, timeout

    def chat_json(self, system, user, schema, max_tokens=1500):
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}},
                "chat_template_kwargs": {"enable_thinking": False}}
        t0 = time.time()
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                js = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"LLM {e.code}: {e.read()[:300]!r}") from e
        ch = js["choices"][0]
        usage = js.get("usage", {})
        meta = {"seconds": round(time.time() - t0, 3), "finish": ch.get("finish_reason"),
                "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")}
        return json.loads(ch["message"]["content"]), meta
