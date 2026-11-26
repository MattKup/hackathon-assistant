"""Собрать ui/quality.json из прогонов эталона (hackathon-runs/runs/*-answer-eval/metrics.json).

    python3 scripts/eval/export_quality.py <путь к hackathon-runs> [id прогона ...]
Только агрегаты, без текста документов.
"""
import json
import sys
from pathlib import Path

NAMES = {"single_dept": "Вопрос по одной дирекции", "needs_clarification": "Нужно уточнить дирекцию",
         "same_across": "Одинаково во всех дирекциях", "complex_project": "Проект нескольких дирекций",
         "dont_know_dept": "Сотрудник не знает дирекцию", "caveat": "Ответ с оговоркой", "external_ref": "Отсылка к документу вне базы",
         "no_answer": "Нет в регламентах → письмо в службу качества", "out_of_scope": "Не про регламенты → отказ",
         "task_request": "«Сделай за меня» → отказ", "find_document": "Где регламент"}


def main(runs_repo, *ids):
    runs = Path(runs_repo) / "runs"
    dirs = [runs / i for i in ids] if ids else sorted(runs.glob("*-answer-eval"))
    out = []
    for d in dirs:
        mf, jf = d / "metrics.json", runs.parent / "jobs" / "done" / f"{d.name}.json"
        if not mf.exists():
            continue
        m = json.loads(mf.read_text())
        if m.get("questions", 0) < 100:
            continue
        note = json.loads(jf.read_text()).get("note", "") if jf.exists() else ""
        meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        cls = {}
        for k, v in m.get("by_class", {}).items():
            ok, tot = map(int, v.split("/"))
            cls[NAMES.get(k, k)] = {"ok": ok, "total": tot}
        out.append({"id": d.name, "date": d.name[:8], "time_utc": d.name[9:15], "note": note,
                    "commit": (meta.get("solution_commit") or "")[:7], "questions": m["questions"],
                    "outcome_ok": m["outcome_ok"], "extra_clarify": m.get("extra_clarify", 0),
                    "cite_hit": m.get("cite_hit"), "cite_total": (m.get("cite_hit") or 0) + (m.get("cite_miss") or 0),
                    "p50": m.get("p50_s"), "p95": m.get("p95_s"), "by_class": cls})
    out.sort(key=lambda r: r["id"], reverse=True)
    q = {"runs": out,
         "stack": [["Qwen3.8", "модель ответа, локально (vLLM)"], ["bge-m3", "поиск по смыслу"],
                   ["bge-reranker-v2-m3", "ранжирование фрагментов"], ["doc-intel", "разбор и распознавание документов, собственная разработка"],
                   ["LightOnOCR-2-1B", "OCR сканов"], ["Mailpit", "имитация почты службы качества"]]}
    Path(__file__).resolve().parents[2].joinpath("ui", "quality.json").write_text(json.dumps(q, ensure_ascii=False, indent=2))
    print(len(out), "прогонов")


if __name__ == "__main__":
    main(*sys.argv[1:])
