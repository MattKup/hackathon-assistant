"""Поиск на стенде: строит окна из корпуса, считает векторы, гоняет вопросы, пишет запись для replay.

Вывод в RUN_DIR: window_vectors.json, queries.jsonl (вопрос, вектор, кандидаты, скоры реранка,
решение уточнения, топ-10), metrics.json (время).
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.dialog.clarify import decide  # noqa: E402
from app.registry.build import run as build_registry  # noqa: E402
from app.search.hybrid import Hybrid  # noqa: E402
from app.search.tei import TEI  # noqa: E402


def main(corpus_rel, questions_rel):
    inbox = Path(os.environ["INBOX_DIR"])
    run_dir = Path(os.environ.get("RUN_DIR", "."))
    tmp = ROOT / "data" / "stand"
    docs, wins = build_registry(inbox / corpus_rel, tmp)
    windows = [w.to_dict() for w in wins]
    tei = TEI(os.environ["TEI_EMBED_URL"], os.environ["TEI_RERANK_URL"])
    t0 = time.time()
    vecs = tei.embed([w["path"] + "\n" + w["text"] for w in windows])
    t_index = time.time() - t0
    (run_dir / "window_vectors.json").write_text(json.dumps(
        {"window_ids": [w["window_id"] for w in windows], "vectors": [[round(x, 5) for x in v] for v in vecs]}))
    hy = Hybrid(windows, vecs, embed_fn=tei.embed, rerank_fn=tei.rerank)
    qs = [json.loads(l) for l in open(inbox / questions_rel) if l.strip()]
    times = []
    with open(run_dir / "queries.jsonl", "w") as fh:
        for q in qs:
            t1 = time.time()
            qvec = tei.embed([q["question"]])[0]
            hits, tr = hy.search(q["question"], k=10, qvec=qvec)
            d = decide(q["question"], hits, docs)
            dt = time.time() - t1
            times.append(dt)
            fh.write(json.dumps({"id": q["id"], "question": q["question"], "qvec": [round(x, 5) for x in qvec],
                                 "candidates": tr["candidates"], "rerank_scores": tr["rerank_scores"],
                                 "top10": [[h["window_id"], h["doc_id"][:8], h["clause"], h["score"]] for h in hits],
                                 "decision": {"action": d.action, "options": [o["label"] for o in d.options],
                                              "doc_ids": d.doc_ids}, "seconds": round(dt, 3)}, ensure_ascii=False) + "\n")
    times.sort()
    m = {"windows": len(windows), "index_seconds": round(t_index, 1), "questions": len(qs),
         "search_p50": round(times[len(times) // 2], 3) if times else None,
         "search_p95": round(times[int(len(times) * 0.95) - 1], 3) if times else None,
         "dim": len(vecs[0]) if vecs else 0}
    (run_dir / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=2))
    print(json.dumps(m, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
