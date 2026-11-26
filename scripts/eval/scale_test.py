"""Проверка на масштабе (универсальная версия, 26.11): синтетическая большая база из копий корпуса.

4 документа × COPIES копий = ~300 регламентов, у каждой копии своё подразделение («<название> (копия N)»).
Векторы копий — те же, что у оригинала (текст тот же), поэтому TEI считает только 709 окон. Копии — дистракторы:
вопрос с названной дирекцией должен остаться в её документах; вопрос без дирекции — уйти не дальше MAX_DEPTS дирекций.
Метрики на одних и тех же вопросах: исходная база vs большая — время, поиски, вызовы LLM, токены, длина контекста,
попадание эталонного пункта (по номеру пункта — у копий номера те же), исходы.

Аргументы: <папка корпуса в INBOX_DIR> <eval.jsonl в INBOX_DIR> [копий=75] [вопросов=40]
"""
import json
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.answer.llm import LLM  # noqa: E402
from app.pipeline import run_turn  # noqa: E402
from app.registry.build import run as build_registry  # noqa: E402
from app.search import hybrid  # noqa: E402
from app.search.hybrid import Hybrid  # noqa: E402
from app.search.tei import TEI  # noqa: E402


def clone(docs, wins, copies):
    rdocs, rwins = list(docs), list(wins)
    for c in range(1, copies):
        for d in docs:
            nd = {**d, "doc_id": f"{d['doc_id']}#c{c}", "department": f"{d['department']}_C{c}",
                  "department_name": f"{d.get('department_name', d['department'])} (копия {c})",
                  "display_title": f"{d['display_title']} (копия {c})"}
            rdocs.append(nd)
            for w in wins:
                if w["doc_id"] == d["doc_id"]:
                    rwins.append({**w, "window_id": f"{w['window_id']}#c{c}", "doc_id": nd["doc_id"], "department": nd["department"]})
    return rdocs, rwins


def run(label, docs, wins, vec_of, items, tei, llm):
    t0 = time.time()
    hy = Hybrid(wins, [vec_of[w["window_id"].split("#")[0]] for w in wins], tei.embed, tei.rerank)
    build_s = round(time.time() - t0, 2)
    calls = Counter()

    def search(q, allowed, k=16):
        calls["search"] += 1
        return hy.search(q, k=k, allowed=allowed)[0]
    rows = []
    for e in items:
        calls.clear()
        t1 = time.time()
        try:
            t = run_turn(e["question"], docs, search, llm, session={"no_clarify": True})
            exp = {c["clause"] for v in (e["expected"].get("by_department") or {}).values() for c in v["citations"]}
            got = {s["clause"] for a in t.answers for s in a["result"]["steps"]}
            rows.append({"id": e["id"], "class": e["class"], "outcome": t.outcome, "seconds": round(time.time() - t1, 2),
                         "searches": calls["search"], "tokens": t.tokens, "depts": [a["label"] for a in t.answers],
                         "ctx_chars": max([a["meta"].get("context_chars", 0) for a in t.answers] or [0]),
                         "cite_hit": bool(exp & got) if exp else None})
        except Exception as ex:
            rows.append({"id": e["id"], "class": e["class"], "error": str(ex)[:200], "seconds": round(time.time() - t1, 2)})
        print(label, rows[-1]["id"], rows[-1].get("outcome"), rows[-1]["seconds"], rows[-1].get("searches"), flush=True)
    ok = [r for r in rows if "error" not in r]
    secs = sorted(r["seconds"] for r in ok)
    cite = [r["cite_hit"] for r in ok if r["cite_hit"] is not None]
    return rows, {"docs": len(docs), "windows": len(wins), "departments": len({d["department"] for d in docs}),
                  "index_build_s": build_s, "errors": len(rows) - len(ok),
                  "p50_s": secs[len(secs) // 2] if secs else None, "p95_s": secs[max(0, int(len(secs) * .95) - 1)] if secs else None,
                  "searches_avg": round(sum(r["searches"] for r in ok) / max(1, len(ok)), 1),
                  "searches_max": max([r["searches"] for r in ok] or [0]),
                  "llm_calls_avg": round(sum(r["tokens"].get("calls", 0) for r in ok) / max(1, len(ok)), 2),
                  "prompt_tokens_avg": round(sum(r["tokens"].get("prompt_tokens", 0) for r in ok) / max(1, len(ok))),
                  "ctx_chars_max": max([r["ctx_chars"] for r in ok] or [0]),
                  "depts_answered_max": max([len(r["depts"]) for r in ok] or [0]),
                  "cite_hit": f"{sum(cite)}/{len(cite)}", "outcomes": dict(Counter(r["outcome"] for r in ok))}


def main(corpus_rel, eval_rel, copies="75", nq="40"):
    inbox, run_dir = Path(os.environ["INBOX_DIR"]), Path(os.environ.get("RUN_DIR", "."))
    docs, wins = build_registry(inbox / corpus_rel, ROOT / "data" / "stand")
    wins = [w.to_dict() for w in wins]
    tei = TEI(os.environ["TEI_EMBED_URL"], os.environ["TEI_RERANK_URL"])
    vec_of = {}
    for i in range(0, len(wins), 256):
        part = wins[i:i + 256]
        vec_of.update(zip([w["window_id"] for w in part], tei.embed([w["path"] + "\n" + w["text"] for w in part])))
    llm = LLM(os.environ["LLM_BASE_URL"], os.environ.get("LLM_MODEL", "qwen38"))
    items = [json.loads(l) for l in open(inbox / eval_rel) if l.strip()]
    pick = [e for e in items if e["class"] in ("single_dept", "needs_clarification", "complex_project", "no_answer", "same_across")]
    random.Random(26).shuffle(pick)
    pick = pick[: int(nq)]
    big_docs, big_wins = clone(docs, wins, int(copies))
    pc0 = llm.prefix_cache()
    rows_s, m_small = run("small", docs, wins, vec_of, pick, tei, llm)
    rows_b, m_big = run("big", big_docs, big_wins, vec_of, pick, tei, llm)
    pc1 = llm.prefix_cache()
    m = {"numpy": hybrid.np is not None, "copies": int(copies), "questions": len(pick), "small": m_small, "big": m_big,
         "same_outcome": sum(1 for a, b in zip(rows_s, rows_b) if a.get("outcome") == b.get("outcome")),
         "vllm_prefix_cache": ({"queries": pc1["queries"] - pc0["queries"], "hits": pc1["hits"] - pc0["hits"],
                                "hit_rate": round((pc1["hits"] - pc0["hits"]) / max(1, pc1["queries"] - pc0["queries"]), 3)}
                               if pc0 and pc1 else None)}
    with open(run_dir / "scale.jsonl", "w") as fh:
        for a, b in zip(rows_s, rows_b):
            fh.write(json.dumps({"small": a, "big": b}, ensure_ascii=False) + "\n")
    (run_dir / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=2))
    print(json.dumps(m, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:])
