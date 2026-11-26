"""Воронка качества по прогонам эталона: на каком шаге теряется ответ (локально, stdlib).

    python3 scripts/eval/funnel.py <run_dir> [<run_dir> …] --eval <eval.jsonl> [--judged '<glob>' …] [--split dev|holdout|all]
                                   [--no-english] [--md out.md]

Шаги воронки (для вопросов, где по эталону ответ есть):
  1. поиск   — эталонный пункт в топе выдачи хода (top, до 5);
  2. контекст — эталонный пункт в контексте модели (meta.clauses_seen) или в топе;
  3. цитата  — эталонный пункт процитирован в ответе (шаги);
  4. исход   — исход хода совпал с ожидаемым (outcome_ok прогона);
  5. судья   — по существу верно (если переданы оценки судьи: label correct/correct_refusal).
Отказы: где ответа нет по эталону — верный отказ (escalate/out_of_scope/task_refusal), лишний отказ — где ответ есть.
Причина каждой ошибки: «поиск» (пункта нет в контексте), «модель» (пункт в контексте, ответа/цитаты нет),
«логика» (до модели не дошло: вне регламентов / уточнение / нет ответов по дирекциям), «текст» (цитата есть, судья — нет).
Разрезы: класс, pattern, половина (dev — нечётные id, holdout — чётные). Сравнение прогонов — колонками.
"""
import argparse
import glob
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

REFUSE = {"escalate", "out_of_scope", "task_refusal"}
ANSWERED = {"answer", "answer_with_caveat", "conflict", "document", "external_ref"}


def num(i):
    m = re.search(r"(\d+)$", i)
    return int(m.group(1)) if m else 0


def english(q):
    return not re.search(r"[а-яё]", q.lower())


def load_judged(patterns):
    out = {}
    for p in patterns or []:
        for f in glob.glob(p):
            for line in open(f):
                if line.strip():
                    r = json.loads(line)
                    out[r["id"]] = r
    return out


def analyze(run_dir, gold, judged, split, no_english):
    rows = []
    for line in open(Path(run_dir) / "answers.jsonl"):
        r = json.loads(line)
        g = gold.get(r["id"])
        if not g:
            continue
        if split == "dev" and num(r["id"]) % 2 == 0 or split == "holdout" and num(r["id"]) % 2 == 1:
            continue
        if no_english and english(r["question"]):
            continue
        ex = g["expected"]
        turns = [t for t in (r.get("turn1"), r.get("turn2")) if t]
        final = turns[-1] if turns else {}
        gc = {c["clause"] for v in (ex.get("by_department") or {}).values() for c in v.get("citations", [])}
        top = {x[1] for t in turns for x in (t.get("top") or [])}
        seen = {c for t in turns for a in t.get("answers", []) for c in (a.get("meta") or {}).get("clauses_seen", [])}
        cited = {s.get("clause") for a in final.get("answers", []) for s in a["result"].get("steps", [])}
        has_answer = ex["outcome"] not in ("escalate", "out_of_scope", "task_refusal")
        j = judged.get(r["id"])
        jok = None if j is None else j["label"] in ("correct", "correct_refusal")
        out = final.get("outcome")
        x = {"id": r["id"], "class": r["class"], "pattern": g.get("pattern", "—"), "q": r["question"], "expected": ex["outcome"],
             "outcome": out, "ok": bool(r.get("outcome_ok")), "judge": jok, "label": j["label"] if j else None,
             "critical": bool(j and j.get("critical")), "has_answer": has_answer, "gc": bool(gc),
             "in_top": bool(gc & top), "in_ctx": bool(gc & (seen | top)), "cited": bool(gc & cited),
             "seconds": r.get("seconds")}
        # причина ошибки (по судье, если есть, иначе по исходу)
        bad = (jok is False) if jok is not None else not x["ok"]
        if bad:
            if not has_answer:
                x["why"] = "отказ не тот" if out in REFUSE else "ответ там, где ответа нет"
            elif gc and not x["in_ctx"]:
                x["why"] = "поиск"
            elif out in ("out_of_scope", "clarify", "ask") or not final.get("answers"):
                x["why"] = "логика"                  # до ответа модели не дошло или ответ подменён решением
            elif gc and not x["cited"]:
                x["why"] = "модель"                  # пункт у модели был, ответа/цитаты нет
            else:
                x["why"] = "текст ответа"
        rows.append(x)
    return rows


def summary(rows):
    n = len(rows)
    ans = [x for x in rows if x["has_answer"] and x["gc"]]
    no = [x for x in rows if not x["has_answer"]]
    jd = [x for x in rows if x["judge"] is not None]
    s = {"вопросов": n,
         "исход верен": f"{sum(x['ok'] for x in rows)}/{n}",
         "1 поиск (пункт в топе)": f"{sum(x['in_top'] for x in ans)}/{len(ans)}",
         "2 контекст (пункт у модели)": f"{sum(x['in_ctx'] for x in ans)}/{len(ans)}",
         "3 цитата (пункт в ответе)": f"{sum(x['cited'] for x in ans)}/{len(ans)}",
         "лишний отказ (ответ есть)": f"{sum(x['outcome'] in REFUSE for x in ans)}/{len(ans)}",
         "верный отказ (ответа нет)": f"{sum(x['outcome'] in REFUSE for x in no)}/{len(no)}"}
    if jd:
        s["5 судья: по существу верно"] = f"{sum(x['judge'] for x in jd)}/{len(jd)}"
        s["выдумки (fabrication)"] = sum(x["label"] == "fabrication" for x in jd)
        s["критичные"] = sum(x["critical"] for x in jd)
    s["причины ошибок"] = dict(Counter(x["why"] for x in rows if x.get("why")).most_common())
    t = sorted(x["seconds"] for x in rows if x.get("seconds"))
    if t:
        s["p50 / p95, с"] = f"{t[len(t) // 2]:.1f} / {t[max(0, int(len(t) * .95) - 1)]:.1f}"
    return s


def by(rows, key):
    d = defaultdict(list)
    for x in rows:
        d[x[key]].append(x)
    ok = (lambda x: x["judge"]) if any(x["judge"] is not None for x in rows) else (lambda x: x["ok"])
    return {k: f"{sum(bool(ok(x)) for x in v)}/{len(v)}" for k, v in sorted(d.items())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--eval", required=True, action="append")
    ap.add_argument("--judged", action="append", help="glob оценок судьи; по одному на прогон, в том же порядке")
    ap.add_argument("--split", default="all", choices=["all", "dev", "holdout"])
    ap.add_argument("--no-english", action="store_true")
    ap.add_argument("--md")
    a = ap.parse_args()
    gold = {}
    for f in a.eval:
        gold.update({json.loads(l)["id"]: json.loads(l) for l in open(f) if l.strip()})
    js = a.judged or []
    res = []
    for i, run in enumerate(a.runs):
        rows = analyze(run, gold, load_judged([js[i]] if i < len(js) and js[i] != "-" else []), a.split, a.no_english)
        res.append((Path(run).name, rows))
    keys = list(summary(res[0][1]).keys())
    sums = [summary(rows) for _, rows in res]
    keys += [k for s in sums for k in s if k not in keys]
    L = [f"# Воронка качества (половина: {a.split}{', без английских' if a.no_english else ''})", "",
         "| Показатель | " + " | ".join(n for n, _ in res) + " |", "|---|" + "---|" * len(res)]
    for k in keys:
        L.append(f"| {k} | " + " | ".join(str(s.get(k, "—")) for s in sums) + " |")
    for key, title in (("class", "По классам"), ("pattern", "По видам вопросов")):
        parts = [by(rows, key) for _, rows in res]
        ks = sorted({k for p in parts for k in p})
        L += ["", f"## {title} (судья, если есть, иначе исход)", "", "| | " + " | ".join(n for n, _ in res) + " |",
              "|---|" + "---|" * len(res)]
        L += [f"| {k} | " + " | ".join(p.get(k, "—") for p in parts) + " |" for k in ks]
    name, rows = res[-1]
    L += ["", f"## Ошибки последнего прогона ({name}) по причине", ""]
    for why in Counter(x["why"] for x in rows if x.get("why")):
        L.append(f"**{why}:** " + "; ".join(f"{x['id']} ({x['expected']}→{x['outcome']})" for x in rows if x.get("why") == why))
        L.append("")
    text = "\n".join(L)
    if a.md:
        Path(a.md).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
