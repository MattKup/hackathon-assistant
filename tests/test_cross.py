"""Г17: отбор дирекций для перекрёстного прохода (app.pipeline.cross_targets)."""
import unittest

from app.pipeline import CROSS_SUPPORT, cross_targets


def _r(dep, verdict, deep=None, **res):
    return {"doc_id": dep, "label": dep, "windows": [{"window_id": dep + "1"}],
            "result": {"verdict": verdict, "steps": [], "rejected": [], **res}, "meta": ({"deep": deep} if deep else {})}


class CrossTargetsTest(unittest.TestCase):
    def test_only_where_no_second_look_yet(self):
        results = [_r("IB", "answer"),
                   _r("TECH", "no_answer", deep={"better": False}),      # агент уже копнул глубже, модель дважды сказала «нет»
                   _r("DBRCT", "no_answer")]                              # второго взгляда не было, опора есть
        got = cross_targets(results, {"IB": 0.9, "TECH": 0.8, "DBRCT": 0.4})
        self.assertEqual([r["doc_id"] for r in got], ["DBRCT"])

    def test_weak_support_skipped(self):
        results = [_r("IB", "answer"), _r("DBRCT", "no_answer")]
        self.assertEqual(cross_targets(results, {"IB": 0.9, "DBRCT": CROSS_SUPPORT - 0.01}), [])
        self.assertEqual(cross_targets(results, {}), [])

    def test_downgraded_by_quote_check_retried(self):
        """Модель ответила, шаги сняты проверкой цитат (v4_052) — повтор нужен, даже если глубокий проход уже был."""
        results = [_r("IB", "answer"), _r("TECH", "no_answer", deep={"better": False}, downgraded="все шаги без проверенной опоры")]
        self.assertEqual([r["doc_id"] for r in cross_targets(results, {"TECH": 0.9})], ["TECH"])

    def test_model_failure_and_empty_context_skipped(self):
        results = [_r("IB", "answer"), _r("TECH", "no_answer", error="timeout"), {**_r("DBRCT", "no_answer"), "windows": []}]
        self.assertEqual(cross_targets(results, {"TECH": 0.9, "DBRCT": 0.9}), [])
