"""Клиент TEI (Text Embeddings Inference): /embed и /rerank.

Формат по документации TEI (router/src/http/types.rs): EmbedRequest{inputs, truncate, normalize}
→ список векторов; RerankRequest{query, texts, truncate, raw_scores} → [{index, score}].
"""
import json
import time
import urllib.error
import urllib.request


def _post(url, body, timeout=120, retries=3):
    data = json.dumps(body).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt + 1 < retries:      # перегрузка — пауза и повтор
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"TEI {url} {e.code}: {e.read()[:300]!r}") from e


class TEI:
    def __init__(self, embed_url, rerank_url, batch=32):
        self.embed_url = embed_url.rstrip("/")
        self.rerank_url = rerank_url.rstrip("/")
        self.batch = batch

    def embed(self, texts):
        out = []
        for i in range(0, len(texts), self.batch):
            out += _post(self.embed_url + "/embed", {"inputs": texts[i:i + self.batch], "truncate": True, "normalize": True})
        return out

    def rerank(self, query, texts, batch=8):
        """Реранкер стенда принимает ≤ 8 текстов за запрос (422 «batch size > 8», 26.11)."""
        scores = [0.0] * len(texts)
        for i in range(0, len(texts), batch):
            res = _post(self.rerank_url + "/rerank",
                        {"query": query, "texts": texts[i:i + batch], "truncate": True, "raw_scores": False})
            for r in res:
                scores[i + r["index"]] = r["score"]
        return scores
