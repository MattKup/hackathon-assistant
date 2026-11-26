"""Прогон эталона на стенде: первый ход по каждому вопросу (+ второй ход для clarify: выбор первого
ожидаемого подразделения). Пишет answers.jsonl (полные ответы — для судьи) и metrics.json.

Аргументы: <папка корпуса в INBOX_DIR> <eval.jsonl в INBOX_DIR> [лимит] [--no-clarify]

Режимы (решение владельца 26.11 — жюри может не нажимать кнопки):
  clarify (по умолчанию) — ассистент уточняет дирекцию, если ответ есть в ≥ 2 дирекциях; второй ход из кэша сессии;
  --no-clarify — флаг сессии no_clarify: сразу ответы по всем дирекциям; вопрос, где эталон ждёт уточнения,
  засчитывается, если дан ответ по ≥ 2 дирекциям (а для уточнения стадии ИБ — ответ с пунктами обоих документов ИБ).
Уточнение стадии ИБ (clarify_dimension=stage, напр. q091/q093/q095): стадию выбирает система — ответ, где
процитированы пункты обоих документов ИБ (пресейл и проект), засчитывается в обоих режимах (решение 26.11).
"""
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.answer.llm import LLM  # noqa: E402
from app.pipeline import load_conflicts, run_turn  # noqa: E402
from app.registry.build import run as build_registry  # noqa: E402
from app.search.hybrid import Hybrid  # noqa: E402
from app.search.tei import TEI  # noqa: E402

EXPECT = {"answer": {"answer", "answer_with_caveat"}, "clarify": {"clarify"},
          "answer_per_department": {"answer", "answer_with_caveat", "conflict"}, "compare_all": {"clarify"},
          "answer_with_caveat": {"answer_with_caveat", "conflict"}, "external_ref": {"external_ref", "answer_with_caveat"},
          "escalate": {"escalate"}, "out_of_scope": {"out_of_scope"}, "task_refusal": {"task_refusal"},
          "document": {"document"}}


ANSWERED = {"answer", "answer_with_caveat", "conflict"}


def answered_depts(t):
    return [a["label"] for a in t.answers if a["result"]["verdict"] != "no_answer"]


def both_ib_stages(t, docs):
    ib = {d["display_title"]: d["stage"] for d in docs if d["department"] == "IB"}
    cited = {ib[s["doc_title"]] for a in t.answers for s in a["result"]["steps"] if s.get("doc_title") in ib}
    return {"presale", "project"} <= cited


def main(corpus_rel, eval_rel, *rest):
    no_clarify = "--no-clarify" in rest
    rest = [r for r in rest if not r.startswith("--")]
    limit = rest[0] if rest else None
    inbox, run_dir = Path(os.environ["INBOX_DIR"]), Path(os.environ.get("RUN_DIR", "."))
    docs, wins = build_registry(inbox / corpus_rel, ROOT / "data" / "stand")
    windows = [w.to_dict() for w in wins]
    dept_doc = {}
    for d in docs:
        dept_doc.setdefault(d["department"], []).append(d["doc_id"])
    tei = TEI(os.environ["TEI_EMBED_URL"], os.environ["TEI_RERANK_URL"])
    hy = Hybrid(windows, tei.embed([w["path"] + "\n" + w["text"] for w in windows]), tei.embed, tei.rerank)
    llm = LLM(os.environ["LLM_BASE_URL"], os.environ.get("LLM_MODEL", "qwen38"))
    search = lambda q, allowed, k=16: hy.search(q, k=k, allowed=allowed)[0]  # noqa: E731
    conflicts = load_conflicts(windows)
    items = [json.loads(l) for l in open(inbox / eval_rel) if l.strip()]
    if limit:
        items = items[: int(limit)]
    stats, times, by_class = Counter(), [], Counter()
    pc0 = llm.prefix_cache() if hasattr(llm, "prefix_cache") else None
    with open(run_dir / "answers.jsonl", "w") as fh:
        for e in items:
            q = e["question"]
            t0 = time.time()
            try:
                sess = {"no_clarify": no_clarify}
                t1 = run_turn(q, docs, search, llm, session=sess, conflicts=conflicts)
                row = {"id": e["id"], "class": e["class"], "question": q, "turn1": turn_dict(t1)}
                exp = e["expected"]["outcome"]
                ok = t1.outcome in EXPECT.get(exp, {exp})
                if e["class"] == "same_across" and t1.outcome == "clarify":
                    row["extra_clarify"] = True          # лишнее уточнение: считаем отдельно
                if e["class"] == "complex_project":
                    ok = ok and len(t1.answers) >= 2
                stage_q = e["expected"].get("clarify_dimension") == "stage"
                if stage_q and t1.outcome in ANSWERED and both_ib_stages(t1, docs):
                    ok = True
                    row["stage_both"] = True
                if no_clarify and exp in ("clarify", "compare_all") and not stage_q:
                    ok = t1.outcome in ANSWERED and len(answered_depts(t1)) >= 2
                row["answered_depts"] = answered_depts(t1)
                if t1.outcome == "ask":
                    # режим диалога: уточнение условия/темы. Для классов, где эталон ждёт уточнения, — это уточнение;
                    # для остальных — второй ход с первым вариантом, засчитывается по его исходу (отдельно считаем asked)
                    row["asked"] = [o["label"] for o in t1.options]
                    sess["ask_reply"] = t1.options[0]["label"]
                    t2 = run_turn(q, docs, search, llm, session=sess, conflicts=conflicts)
                    row["turn2_choice"], row["turn2"] = t1.options[0]["label"], turn_dict(t2)
                    ok = exp in ("clarify", "compare_all") or t2.outcome in EXPECT.get(exp, {exp})
                    tok2 = t2.tokens
                    t1.tokens = {k: t1.tokens.get(k, 0) + tok2.get(k, 0) for k in t1.tokens}
                row["outcome_ok"] = ok
                if t1.outcome == "clarify" and e["class"] == "dont_know_dept":
                    alld = [d for o in t1.options if o["doc_id"] not in ("*", "?") for d in dept_doc.get(o["doc_id"], [])]
                    sess.update(docs=alld, clarifications=1)
                    t2 = run_turn(q, docs, search, llm, session=sess, conflicts=conflicts)
                    t1.tokens = {k: t1.tokens.get(k, 0) + t2.tokens.get(k, 0) for k in t1.tokens}
                    row["turn2_choice"] = "не знаю → все варианты"
                    row["turn2"] = turn_dict(t2)
                    row["outcome_ok"] = ok = ok and len(t2.answers) >= 2
                elif t1.outcome == "clarify" and e["expected"].get("clarify_options"):
                    dep = e["expected"]["clarify_options"][0]
                    sess.update(docs=dept_doc.get(dep, []), clarifications=1)
                    t2 = run_turn(q, docs, search, llm, session=sess, conflicts=conflicts)
                    t1.tokens = {k: t1.tokens.get(k, 0) + t2.tokens.get(k, 0) for k in t1.tokens}
                    row["turn2_choice"] = dep
                    row["turn2"] = turn_dict(t2)
                exp_clauses = {c["clause"] for v in (e["expected"].get("by_department") or {}).values() for c in v["citations"]}
                got = {s["clause"] for a in (row.get("turn2") or row["turn1"])["answers"] for s in a["result"]["steps"]}
                row["cite_hit"] = bool(exp_clauses & got) if exp_clauses else None
                row["tokens"] = t1.tokens
                for k, v in t1.tokens.items():
                    stats["tok_" + k] += v
                stats["asked"] += 1 if row.get("asked") else 0
                stats["auto_mail"] += 1 if (t1.route or {}).get("email") and t1.outcome != "escalate" else 0
            except Exception as ex:  # сбой одного вопроса не останавливает прогон
                row = {"id": e["id"], "class": e["class"], "question": q, "error": str(ex)[:300], "outcome_ok": False}
                stats["errors"] += 1
            row["seconds"] = round(time.time() - t0, 2)
            times.append(row["seconds"])
            stats["ok" if row["outcome_ok"] else "bad"] += 1
            stats["extra_clarify"] += 1 if row.get("extra_clarify") else 0
            by_class[(e["class"], row["outcome_ok"])] += 1
            if row.get("cite_hit") is not None:
                stats["cite_hit" if row["cite_hit"] else "cite_miss"] += 1
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(e["id"], e["class"], row.get("turn1", {}).get("outcome"), row["outcome_ok"], row["seconds"], flush=True)
    times.sort()
    classes = sorted({c for c, _ in by_class})
    pc1 = llm.prefix_cache() if pc0 else None
    m = {"vllm_prefix_cache": ({"queries": pc1["queries"] - pc0["queries"], "hits": pc1["hits"] - pc0["hits"],
                                "hit_rate": round((pc1["hits"] - pc0["hits"]) / max(1, pc1["queries"] - pc0["queries"]), 3)}
                               if pc0 and pc1 else None),
         "mode": "no_clarify" if no_clarify else "clarify", "questions": len(items), "outcome_ok": stats["ok"], "outcome_bad": stats["bad"], "errors": stats["errors"],
         "cite_hit": stats["cite_hit"], "cite_miss": stats["cite_miss"], "extra_clarify": stats["extra_clarify"],
         "asked": stats["asked"], "auto_mail_not_covered": stats["auto_mail"],
         "tokens_total": {k[4:]: stats[k] for k in stats if k.startswith("tok_")},
         "tokens_per_question": {k[4:]: round(stats[k] / max(1, len(items))) for k in stats if k.startswith("tok_")},
         "by_class": {c: f"{by_class[(c, True)]}/{by_class[(c, True)] + by_class[(c, False)]}" for c in classes},
         "p50_s": times[len(times) // 2] if times else None, "p95_s": times[max(0, int(len(times) * .95) - 1)] if times else None}
    (run_dir / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=2))
    print(json.dumps(m, ensure_ascii=False, indent=2))


def turn_dict(t):
    return {"outcome": t.outcome, "text": t.text, "tokens": t.tokens, "options": [o["label"] for o in t.options], "route": t.route,
            "documents": t.documents, "external_refs": t.external_refs,
            "trace": t.trace, "top": [[h["department"], h["clause"], h["score"]] for h in t.hits[:5]],
            "answers": [{"label": a["label"], "result": a["result"], "meta": a["meta"]} for a in t.answers]}  # windows не пишем


if __name__ == "__main__":
    main(*sys.argv[1:])
