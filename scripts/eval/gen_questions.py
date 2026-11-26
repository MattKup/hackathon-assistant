"""Г14, ночная задача: вопросы-«входы» к каждому пункту корпуса (локальная LLM). Пишет questions.jsonl и metrics.json в RUN_DIR.
Аргументы: <папка корпуса в INBOX_DIR> [workers=8]"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.answer.llm import LLM  # noqa: E402
from app.registry.build import run as build_registry  # noqa: E402
from app.search.questions import generate  # noqa: E402


def main(corpus_rel, *rest):
    kv = dict(r.split("=", 1) for r in rest if "=" in r)
    inbox, run_dir = Path(os.environ["INBOX_DIR"]), Path(os.environ.get("RUN_DIR", "."))
    docs, wins = build_registry(inbox / corpus_rel, ROOT / "data" / "gen")
    names = {d["doc_id"]: d.get("department_name") for d in docs}
    ws = [{**w.to_dict(), "department_name": names.get(w.doc_id)} for w in wins]
    ws = [w for w in ws if len(w["text"]) >= 80 and w.get("kind") != "section_title"]
    llm = LLM(os.environ["LLM_BASE_URL"], os.environ.get("LLM_MODEL", "qwen38"), timeout=120)
    t0, tok, errors = time.time(), [0, 0], 0

    def one(w):
        try:
            qs, meta = generate(llm, w)
            return w, qs, meta, None
        except Exception as e:
            return w, [], {}, str(e)[:120]
    n = 0
    with open(run_dir / "questions.jsonl", "w") as fh, ThreadPoolExecutor(max_workers=int(kv.get("workers", 8))) as ex:
        for w, qs, meta, err in ex.map(one, ws):
            errors += bool(err)
            tok[0] += meta.get("prompt_tokens") or 0
            tok[1] += meta.get("completion_tokens") or 0
            n += len(qs)
            fh.write(json.dumps({"window_id": w["window_id"], "doc_id": w["doc_id"], "clause": w["clause"], "questions": qs,
                                 **({"error": err} if err else {})}, ensure_ascii=False) + "\n")
    m = {"windows": len(ws), "questions": n, "errors": errors, "prompt_tokens": tok[0], "completion_tokens": tok[1],
         "seconds": round(time.time() - t0, 1), "corpus": corpus_rel}
    (run_dir / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=2))
    print(json.dumps(m, ensure_ascii=False))


if __name__ == "__main__":
    main(*sys.argv[1:])
