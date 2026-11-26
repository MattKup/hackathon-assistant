"""Гибридный поиск: BM25 + плотный (bge-m3) через RRF, затем реранкер по топ-N.

Векторы окон считаются один раз (индекс) и могут подаваться из записи (replay),
поэтому класс не зависит от сети: embed_fn и rerank_fn передаются снаружи.
"""
from app.search.bm25 import BM25


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


class Hybrid:
    def __init__(self, windows, vectors, embed_fn=None, rerank_fn=None, rrf_k=60):
        self.windows = windows
        self.vectors = vectors                 # список векторов в порядке windows
        self.bm25 = BM25(windows)
        self.embed_fn, self.rerank_fn, self.rrf_k = embed_fn, rerank_fn, rrf_k
        self.pos = {w["window_id"]: i for i, w in enumerate(windows)}

    def search(self, query, k=10, pool=40, rerank_n=16, allowed=None, qvec=None, rerank_scores=None):
        """allowed — множество doc_id (фильтр подразделения) применяется ДО ранжирования."""
        trace = {}
        bm = self.bm25.search(query, k=pool, allowed=allowed)
        rank = {}
        for r, h in enumerate(bm):
            rank[h["window_id"]] = rank.get(h["window_id"], 0) + 1 / (self.rrf_k + r + 1)
        if qvec is None and self.embed_fn:
            qvec = self.embed_fn([query])[0]
        if qvec is not None:
            dense = sorted(((dot(qvec, v), i) for i, v in enumerate(self.vectors)
                            if allowed is None or self.windows[i]["doc_id"] in allowed), reverse=True)[:pool]
            for r, (_, i) in enumerate(dense):
                wid = self.windows[i]["window_id"]
                rank[wid] = rank.get(wid, 0) + 1 / (self.rrf_k + r + 1)
            trace["dense"] = True
        fused = sorted(rank.items(), key=lambda x: -x[1])[:rerank_n]
        cands = [self.windows[self.pos[w]] for w, _ in fused]
        if rerank_scores is None and self.rerank_fn and cands:
            rerank_scores = self.rerank_fn(query, [c["path"] + "\n" + c["text"] for c in cands])
        if rerank_scores is not None:
            order = sorted(range(len(cands)), key=lambda i: -rerank_scores[i])
            out = [{**cands[i], "score": round(float(rerank_scores[i]), 4)} for i in order[:k]]
            trace["rerank"] = True
        else:
            out = [{**c, "score": round(s, 5)} for c, (_, s) in zip(cands, fused)][:k]
        return out, {**trace, "candidates": [c["window_id"] for c in cands], "rerank_scores": rerank_scores}
