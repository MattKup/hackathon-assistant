"""Поиск противоречий в корпусе на стенде (LLM qwen38 + эмбеддинги bge-m3 через TEI).

Аргументы: <папка корпуса в INBOX_DIR> [бюджет пар, по умолчанию 300] [k=8] [k_cross=3] [workers=4]
           [cross_share=0.2] [--no-dense] [--offline]
Пишет в RUN_DIR: conflicts.json, conflicts.md, metrics.json. Без стенда: --offline (сигналы кода, без LLM)
и INBOX_DIR — любая папка, где лежит корпус.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.analysis.conflicts import analyze, write_outputs  # noqa: E402
from app.registry.build import run as build_registry  # noqa: E402


def main(corpus_rel, *rest):
    flags = {r for r in rest if r.startswith("--")}
    kv = dict(r.split("=", 1) for r in rest if "=" in r and not r.startswith("--"))
    pos = [r for r in rest if not r.startswith("--") and "=" not in r]
    budget = int(pos[0]) if pos else 300
    inbox, run_dir = Path(os.environ["INBOX_DIR"]), Path(os.environ.get("RUN_DIR", "."))
    docs, wins = build_registry(inbox / corpus_rel, ROOT / "data" / "conflicts")
    windows = [w.to_dict() for w in wins]
    llm, vectors = None, None
    if "--offline" not in flags:
        from app.answer.llm import LLM
        llm = LLM(os.environ["LLM_BASE_URL"], os.environ.get("LLM_MODEL", "qwen38"), timeout=120)
        if "--no-dense" not in flags and os.environ.get("TEI_EMBED_URL"):
            from app.search.tei import TEI
            tei = TEI(os.environ["TEI_EMBED_URL"], os.environ.get("TEI_RERANK_URL", ""))
            vectors = tei.embed([w["path"] + "\n" + w["text"] for w in windows])
    rep = analyze(windows, docs, llm, budget=budget, k=int(kv.get("k", 8)), k_cross=int(kv.get("k_cross", 3)),
                  cross_share=float(kv.get("cross_share", 0.2)), vectors=vectors, workers=int(kv.get("workers", 4)),
                  log=lambda *a: print(*a, flush=True))
    write_outputs(rep, run_dir)
    m = {**rep["metrics"], "corpus": corpus_rel, "model": os.environ.get("LLM_MODEL", "qwen38") if llm else None}
    (run_dir / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=2))
    print(json.dumps(m, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:])
