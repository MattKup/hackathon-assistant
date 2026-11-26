"""Поиск противоречий в корпусе на стенде (LLM qwen38 + эмбеддинги bge-m3 через TEI).

Аргументы: <папка корпуса в INBOX_DIR> [бюджет пар] [k=8] [k_cross=3] [workers=6] [cross_share=0.2]
           [changed=<префикс doc_id>,...] [prev=<metrics.json|conflicts.json прошлого прогона в INBOX_DIR>]
           [--no-dense] [--offline]

Режимы:
  ночной (по умолчанию, без числа бюджета) — «точность выше стоимости»: все пары после расширенного фильтра,
      мягкий судья (этап 1) + три независимые перепроверки каждой находки (этап 2), итог по большинству;
  с бюджетом (указано число) — быстрый прогон: строгий судья, не больше N пар, без перепроверки;
  инкрементальный — changed=… или prev=…: только пары, где есть пункт изменённого/нового документа
      (prev: документы, чьего doc_id = sha256 файла нет в прошлом прогоне).
Пишет в RUN_DIR: conflicts.json, conflicts.md, metrics.json. Без стенда: --offline (сигналы кода, без LLM).
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.analysis.conflicts import analyze, write_outputs  # noqa: E402
from app.registry.build import run as build_registry  # noqa: E402


def changed_docs(docs, kv, inbox):
    ch = set()
    for pref in filter(None, kv.get("changed", "").split(",")):
        ch |= {d["doc_id"] for d in docs if d["doc_id"].startswith(pref)}
    if kv.get("prev"):
        prev = json.loads((inbox / kv["prev"]).read_text())
        old = set((prev.get("metrics") or prev).get("doc_ids") or [])
        ch |= {d["doc_id"] for d in docs if d["doc_id"] not in old}
    return ch


def main(corpus_rel, *rest):
    flags = {r for r in rest if r.startswith("--")}
    kv = dict(r.split("=", 1) for r in rest if "=" in r and not r.startswith("--"))
    pos = [r for r in rest if not r.startswith("--") and "=" not in r]
    night = not pos
    budget = int(pos[0]) if pos else None
    inbox, run_dir = Path(os.environ["INBOX_DIR"]), Path(os.environ.get("RUN_DIR", "."))
    docs, wins = build_registry(inbox / corpus_rel, ROOT / "data" / "conflicts")
    windows = [w.to_dict() for w in wins]
    changed = changed_docs(docs, kv, inbox) if ("changed" in kv or "prev" in kv) else None
    if changed is not None and not changed:
        print("инкрементальный режим: изменённых документов нет — анализ не нужен", flush=True)
    llm, vectors = None, None
    if "--offline" not in flags:
        from app.answer.llm import LLM
        llm = LLM(os.environ["LLM_BASE_URL"], os.environ.get("LLM_MODEL", "qwen38"), timeout=180)
        if "--no-dense" not in flags and os.environ.get("TEI_EMBED_URL"):
            from app.search.tei import TEI
            tei = TEI(os.environ["TEI_EMBED_URL"], os.environ.get("TEI_RERANK_URL", ""))
            vectors = tei.embed([w["path"] + "\n" + w["text"] for w in windows])
    rep = analyze(windows, docs, llm, budget=budget or 300, k=int(kv.get("k", 8)), k_cross=int(kv.get("k_cross", 3)),
                  cross_share=float(kv.get("cross_share", 0.2)), vectors=vectors,
                  workers=int(kv.get("workers", 6 if night else 4)), log=lambda *a: print(*a, flush=True),
                  night=night and llm is not None, changed=changed)
    write_outputs(rep, run_dir)
    m = {**rep["metrics"], "corpus": corpus_rel, "model": os.environ.get("LLM_MODEL", "qwen38") if llm else None}
    (run_dir / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in m.items() if k != "doc_ids"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:])
