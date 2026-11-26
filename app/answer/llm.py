"""Клиент OpenAI-совместимого API vLLM (стенд: модель qwen38, без режима размышлений)."""
import json
import time
import urllib.error
import urllib.request


class LLM:
    def __init__(self, base_url, model="qwen38", timeout=90):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.root = base_url.rstrip("/").removesuffix("/v1")      # /tokenize и /metrics — в корне сервера vLLM
        self.model, self.timeout = model, timeout
        self._tokenize_ok = True

    def count_tokens(self, system, user):
        """Токены входа под шаблоном чата (vLLM /tokenize: поле count). Недоступно — оценка по знакам (с запасом)."""
        if self._tokenize_ok:
            body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            try:
                req = urllib.request.Request(self.root + "/tokenize", data=json.dumps(body).encode(), method="POST",
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    js = json.loads(r.read())
                return js.get("count") or len(js.get("tokens") or [])
            except Exception:
                self._tokenize_ok = False      # не дёргаем снова; дальше — оценка
        return int((len(system) + len(user)) / 2.2)    # русский текст у Qwen ≈ 2,5–3 знака на токен; 2,2 — с запасом

    def prefix_cache(self):
        """Счётчики префиксного кэша vLLM (/metrics: vllm:prefix_cache_queries, vllm:prefix_cache_hits — в токенах)."""
        try:
            with urllib.request.urlopen(self.root + "/metrics", timeout=5) as r:
                text = r.read().decode()
        except Exception:
            return None
        out = {"queries": 0.0, "hits": 0.0}
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            for k in out:
                if line.startswith(f"vllm:prefix_cache_{k}"):
                    try:
                        out[k] += float(line.rsplit(" ", 1)[1])
                    except ValueError:
                        pass
        return out

    def chat_json(self, system, user, schema, max_tokens=1500, thinking=False):
        """thinking=True — режим рассуждения Qwen (Г10/Г11, 26.11): длиннее вывод и таймаут; ответ — JSON после </think>."""
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens + (6000 if thinking else 0),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}},
                "chat_template_kwargs": {"enable_thinking": bool(thinking)}}
        t0 = time.time()
        for attempt in (1, 2):                 # один повтор при сетевом сбое, таймауте или 5xx (ревью Fable К12)
            req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout * (3 if thinking else 1)) as r:
                    js = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                if e.code >= 500 and attempt == 1:
                    time.sleep(1)
                    continue
                raise RuntimeError(f"LLM {e.code}: {e.read()[:300]!r}") from e
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise
                time.sleep(1)
        ch = js["choices"][0]
        usage = js.get("usage", {})
        meta = {"seconds": round(time.time() - t0, 3), "finish": ch.get("finish_reason"),
                "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")}
        content = ch["message"].get("content") or ""
        if "</think>" in content:
            content = content.split("</think>", 1)[1]
        meta["thinking"] = bool(thinking)
        if thinking:
            # Ночь 27.11: в 12 из 107 ответов (v3) рассуждение съедало бюджет и JSON не приходил («Expecting value»).
            # Откат: тот же запрос без рассуждения — режим не должен давать отказ там, где обычный ответил бы.
            try:
                return json.loads(content.strip()), meta
            except ValueError:
                out, m2 = self.chat_json(system, user, schema, max_tokens=max_tokens, thinking=False)
                m2.update({"thinking_fallback": True, "thinking_finish": meta["finish"],
                           "seconds": round(meta["seconds"] + m2["seconds"], 3),
                           "completion_tokens": (meta["completion_tokens"] or 0) + (m2["completion_tokens"] or 0)})
                return out, m2
        return json.loads(content.strip()), meta
