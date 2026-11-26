"""Реестр документов и окна из папки корпуса (локально, без сети).

    python3 -m app.registry.build <папка_корпуса> <папка_вывода>

Пишет registry.json (документы) и windows.jsonl (окна). Вывод — в data/ (не в git).
"""
import hashlib
import json
import sys
from pathlib import Path

from app.chunking.clauses import build
from app.parsers.docx_simple import parse

CONFIG = Path(__file__).resolve().parents[2] / "config" / "documents.json"


def meta_for(name, cfg):
    for r in cfg["rules"]:
        if r["match"] in name:
            return r["department"], r["stage"]
    return "UNKNOWN", "unknown"


def run(corpus, out):
    cfg = json.loads(CONFIG.read_text())
    out.mkdir(parents=True, exist_ok=True)
    docs, windows = [], []
    for f in sorted(Path(corpus).glob("*.docx")):
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        paras = parse(f)
        dep, stage = meta_for(f.name, cfg)
        doc = {"doc_id": sha, "file_name": f.name, "title": paras[0].text if paras else f.stem,
               "display_title": f.stem, "department": dep, "department_name": cfg["departments"].get(dep, dep),
               "stage": stage, "status": "active", "status_reason": "все 4 документа действующие (СК, брифинг 26.11)",
               "parser": "docx_simple", "paragraphs": len(paras), "chars": sum(len(p.text) for p in paras)}
        ws = build(doc, paras)
        doc["windows"] = len(ws)
        doc["sections"] = len({w.section for w in ws})
        doc["external_refs"] = sorted({r for w in ws for r in w.external_refs})
        if not ws:
            doc["status"] = "error"
            doc["status_reason"] = "0 окон"
        docs.append(doc)
        windows += ws
    (out / "registry.json").write_text(json.dumps(docs, ensure_ascii=False, indent=2))
    with open(out / "windows.jsonl", "w") as fh:
        for w in windows:
            fh.write(json.dumps(w.to_dict(), ensure_ascii=False) + "\n")
    return docs, windows


if __name__ == "__main__":
    d, w = run(Path(sys.argv[1]), Path(sys.argv[2]))
    for doc in d:
        print(f"{doc['department']:6} {doc['stage']:10} окон={doc['windows']:4} разделов={doc['sections']:3} "
              f"знаков={doc['chars']:6} {doc['display_title']}")
    print("всего окон:", len(w))
