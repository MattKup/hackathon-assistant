"""Нагрузочный замер (экономика): сколько запросов в час выдерживает стенд при N одновременных пользователях.

Для каждого уровня параллельности N (по умолчанию 1, 2, 4, 8) — MINUTES минут непрерывных вопросов из эталона
(режим без уточнения: полный ответ по всем дирекциям), кэш ответов выключен. Метрики на уровень: выполнено вопросов,
запросов в час, задержка p50/p95, токены на вопрос, ошибки. Аргументы: <корпус в INBOX_DIR> <eval.jsonl> [минут=3] [уровни=1,2,4,8]
"""
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.answer.llm import LLM  # noqa: E402
from app.pipeline import build_index, load_conflicts, run_turn  # noqa: E402
from app.registry.build import run as build_registry  # noqa: E402
from app.search.hybrid import Hybrid  # noqa: E402
from app.search.tei import TEI  # noqa: E402


def main(corpus_rel, eval_rel, minutes="3", levels="1,2,4,8"):
    inbox, run_dir = Path(os.environ["INBOX_DIR"]), Path(os.environ.get("RUN_DIR", "."))
    docs, wins = build_registry(inbox / corpus_rel, ROOT / "data" / "stand")
    windows = [w.to_dict() for w in wins]
    tei = TEI(os.environ["TEI_EMBED_URL"], os.environ["TEI_RERANK_URL"])
    hy = Hybrid(windows, tei.embed([w["path"] + "\n" + w["text"] for w in windows]), tei.embed, tei.rerank)
    llm = LLM(os.environ["LLM_BASE_URL"], os.environ.get("LLM_MODEL", "qwen38"))
    conflicts, index = load_conflicts(windows), build_index(windows)
    qs = [json.loads(l)["question"] for l in open(inbox / eval_rel) if l.strip()]
    out = {}
    for n in [int(x) for x in levels.split(",")]:
        stop = time.time() + float(minutes) * 60
        lat, tok, err, lock = [], [], [0], threading.Lock()

        def worker(seed):
            rnd = random.Random(seed)
            while time.time() < stop:
                q = rnd.choice(qs)
                t0 = time.time()
                try:
                    t = run_turn(q, docs, lambda qq, allowed, k=16: hy.search(qq, k=k, allowed=allowed)[0], llm,
                                 session={"no_clarify": True}, conflicts=conflicts, index=index)
                    with lock:
                        lat.append(time.time() - t0)
                        tok.append(t.tokens.get("prompt_tokens", 0) + t.tokens.get("completion_tokens", 0))
                except Exception:
                    with lock:
                        err[0] += 1
        t_start = time.time()
        ths = [threading.Thread(target=worker, args=(n * 100 + i,)) for i in range(n)]
        [th.start() for th in ths]
        [th.join() for th in ths]
        wall = time.time() - t_start
        lat.sort()
        out[str(n)] = {"done": len(lat), "errors": err[0], "wall_s": round(wall, 1),
                       "requests_per_hour": round(len(lat) / wall * 3600),
                       "p50_s": round(lat[len(lat) // 2], 2) if lat else None,
                       "p95_s": round(lat[max(0, int(len(lat) * .95) - 1)], 2) if lat else None,
                       "tokens_per_request": round(sum(tok) / max(1, len(tok)))}
        print(n, out[str(n)], flush=True)
    (run_dir / "metrics.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:])
