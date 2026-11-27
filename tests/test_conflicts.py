"""Анализатор противоречий на синтетике: кандидаты, вид по отношению пар, проверка цитат, бюджет."""
import re
import tempfile
import unittest
from pathlib import Path

from app.analysis import conflicts as C

DOCS = [
    {"doc_id": "tech", "display_title": "Регламент Т", "department": "TECH", "department_name": "Тех дирекция", "stage": "full_cycle"},
    {"doc_id": "dbr", "display_title": "Регламент Д", "department": "DBRCT", "department_name": "ДБРЦТ", "stage": "full_cycle"},
]
FILLER = [
    "Аккаунт-менеджер готовит ТКП и направляет его заказчику после согласования с SS.",
    "Архитектор проводит обследование инфраструктуры заказчика и готовит отчёт.",
    "Установочное совещание проводит Руководитель проекта с участием команды.",
    "Документация по проекту выкладывается в соответствии с матрицей документооборота.",
    "Оплата выполненных работ контролируется Аккаунт-менеджером по графику платежей.",
    "Обратная связь от заказчика собирается анкетированием по завершении проекта.",
]


def win(doc, clause, text, dep=None, section=None):
    d = next(x for x in DOCS if x["doc_id"] == doc)
    return {"window_id": f"{doc}-{clause}", "doc_id": doc, "department": dep or d["department"], "stage": d["stage"],
            "section": section or clause.split(".")[0], "section_title": "Раздел " + clause.split(".")[0],
            "clause": clause, "path": f"{doc} › п. {clause}", "text": f"{clause}. {text}", "kind": "clause",
            "external_refs": []}


def corpus(extra):
    ws = [win("tech", f"1{i}.1", t) for i, t in enumerate(FILLER)]
    ws += [win("dbr", f"2{i}.1", t) for i, t in enumerate(FILLER)]
    return ws + extra


def parts(user):
    m = re.search(r"Пункт A — [^\n]*\n(.*?)\n\nПункт B — [^\n]*\n(.*?)\n\n(?:Сравни|Перепроверь)", user, re.S)
    return m.group(1), m.group(2)


YES = {"reason": "сроки разные", "same_action": "yes", "same_case": "yes", "both_possible": "no", "explicit_exception": "no"}
NO = {"reason": "совместимы", "same_action": "yes", "same_case": "yes", "both_possible": "yes", "explicit_exception": "no"}


class Stub:
    """Заглушка LLM: решение — функция от текстов A и B."""

    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def chat_json(self, system, user, schema, max_tokens=600):
        self.calls += 1
        a, b = parts(user)
        if system == C.CHECK_SYSTEM:
            self.verify_calls = getattr(self, "verify_calls", 0) + 1
            return self.verifier(a, b, system), {"prompt_tokens": 50, "completion_tokens": 10}
        return self.fn(a, b), {"prompt_tokens": 100, "completion_tokens": 20}

    verifier = staticmethod(lambda a, b, system: NO)


def first_sentence(t):
    return C.sentences(t)[0]


def deadline_judge(a, b):
    if "рабочих дней" in a and "рабочих дней" in b and a != b:
        return {"kind": "contradiction", "situation": "создание Сметы", "quote_a": first_sentence(a),
                "quote_b": first_sentence(b), "explanation": "Разные сроки создания Сметы."}
    return {"kind": "none", "situation": "", "quote_a": "", "quote_b": "", "explanation": ""}


SMETA_3 = "После подписания договора РП создаёт Смету в учётной системе в течение 3 рабочих дней."
SMETA_5 = "После подписания договора РП создаёт Смету в учётной системе в течение 5 рабочих дней."


class ConflictsTest(unittest.TestCase):
    def run_an(self, ws, judge, **kw):
        llm = Stub(judge)
        rep = C.analyze(ws, DOCS, llm, workers=1, log=lambda *_: None, **kw)
        return rep, llm

    def test_finds_different_deadlines_same_action(self):
        ws = corpus([win("tech", "5.2", SMETA_3), win("tech", "9.4", SMETA_5)])
        rep, _ = self.run_an(ws, deadline_judge)
        cs = [f for f in rep["findings"] if f["kind"] == "contradiction"]
        self.assertEqual(len(cs), 1)
        self.assertEqual({cs[0]["a"]["clause"], cs[0]["b"]["clause"]}, {"5.2", "9.4"})
        self.assertIn("deadline_diff", cs[0]["signals"])
        self.assertTrue(C.norm(cs[0]["a"]["quote"]) in C.norm(next(w["text"] for w in ws if w["clause"] == cs[0]["a"]["clause"])))

    def test_different_departments_not_contradiction(self):
        ws = corpus([win("tech", "5.2", SMETA_3), win("dbr", "9.4", SMETA_5)])
        rep, _ = self.run_an(ws, deadline_judge)
        kinds = [f["kind"] for f in rep["findings"]]
        self.assertNotIn("contradiction", kinds)
        self.assertIn("dept_difference", kinds)
        f = next(f for f in rep["findings"] if f["kind"] == "dept_difference")
        self.assertEqual(f["relation"], "cross_dept")

    def test_rejects_non_verbatim_quote(self):
        def liar(a, b):
            r = deadline_judge(a, b)
            if r["kind"] != "none":
                r["quote_a"] = "Руководитель проекта обязан мгновенно завести смету за один час"
            return r
        ws = corpus([win("tech", "5.2", SMETA_3), win("tech", "9.4", SMETA_5)])
        rep, _ = self.run_an(ws, liar)
        self.assertEqual([f for f in rep["findings"] if f["kind"] == "contradiction"], [])
        self.assertEqual(rep["metrics"]["rejected_by_reason"].get("quote_not_verbatim"), 1)

    def test_swapped_quotes_are_accepted(self):
        def swap(a, b):
            r = deadline_judge(a, b)
            r["quote_a"], r["quote_b"] = r["quote_b"], r["quote_a"]
            return r
        ws = corpus([win("tech", "5.2", SMETA_3), win("tech", "9.4", SMETA_5)])
        rep, _ = self.run_an(ws, swap)
        self.assertEqual(len([f for f in rep["findings"] if f["kind"] == "contradiction"]), 1)

    def test_quote_with_ellipsis(self):
        text = "10.1. После подписания договора (или исключения) РП создаёт проект в системе управления проектами."
        q, _ = C.verify_quote("После подписания договора ... РП создаёт проект в системе управления проектами", text)
        self.assertIsNotNone(q)
        fixed, repaired = C.verify_quote("РП создаёт проект ... После подписания договора", text)
        self.assertTrue(repaired)                     # порядок нарушен → заменена предложением источника
        self.assertIn(C.norm(fixed), C.norm(text))

    def test_number_not_in_source_rejected(self):
        def wrong_num(a, b):
            r = deadline_judge(a, b)
            if r["kind"] != "none":
                r["explanation"] = "В одном пункте 3 дня, в другом 7 дней."
            return r
        ws = corpus([win("tech", "5.2", SMETA_3), win("tech", "9.4", SMETA_5)])
        rep, _ = self.run_an(ws, wrong_num)
        self.assertEqual(rep["metrics"]["rejected_by_reason"].get("number_not_in_source"), 1)

    def test_budget_respected(self):
        extra = [win("tech", f"{30 + i}.1", f"После подписания договора РП создаёт Смету в учётной системе в течение {i + 1} рабочих дней.")
                 for i in range(8)]
        ws = corpus(extra)
        rep, llm = self.run_an(ws, deadline_judge, budget=3)
        self.assertEqual(llm.calls, 3)
        self.assertEqual(rep["metrics"]["llm_calls"], 3)
        self.assertGreater(rep["metrics"]["passed_filter"], 3)
        self.assertEqual(rep["metrics"]["prompt_tokens"], 300)

    def test_prohibition_vs_permission_signal(self):
        fa = C.features("9.2. Привлечение РП возможно только при подписанном договоре.")
        fb = C.features("7.4. SS в обязательном порядке привлекает РП к согласованию договора.")
        sig, _ = C.signals(fa, fb)
        self.assertIn("obligation_vs_prohibition", sig)
        fc = C.features("5.2.2. Приоритет заявки не меняется, если не выдержан SLA.")
        fd = C.features("5.2.3. Если SS нарушает срок, ПА может понизить приоритет заявки.")
        sig2, _ = C.signals(fc, fd)
        self.assertIn("modality_opposite", sig2)

    def test_offline_mode_and_report(self):
        ws = corpus([win("tech", "5.2", SMETA_3), win("tech", "9.4", SMETA_5)])
        rep = C.analyze(ws, DOCS, None, log=lambda *_: None)
        self.assertEqual(rep["metrics"]["mode"], "offline")
        self.assertNotIn("contradiction", rep["metrics"]["findings_by_kind"])     # без LLM — только «возможные»
        with tempfile.TemporaryDirectory() as d:
            C.write_outputs(rep, Path(d))
            md = (Path(d) / "conflicts.md").read_text()
            self.assertIn("п. 5.2", md)
            self.assertTrue((Path(d) / "conflicts.json").exists())


class NightModeTest(unittest.TestCase):
    def test_review_keeps_confirmed_and_moves_rejected(self):
        ws = corpus([win("tech", "5.2", SMETA_3), win("tech", "9.4", SMETA_5),
                     win("tech", "6.1", "После подписания договора РП создаёт Смету в учётной системе в течение 2 рабочих дней."),
                     win("tech", "6.2", "После подписания договора РП создаёт Смету в учётной системе в течение 7 рабочих дней.")])
        llm = Stub(deadline_judge)
        # перепроверка подтверждает только пару 5.2 ↔ 9.4; адвокат её не защитил
        llm.verifier = lambda a, b, system: YES if ("3 рабочих" in a + b and "5 рабочих" in a + b) else NO
        rep = C.analyze(ws, DOCS, llm, budget=1, workers=1, log=lambda *_: None, night=True)
        m = rep["metrics"]
        self.assertTrue(m["night"])
        self.assertGreater(m["llm_calls"], 1)                      # бюджет ночью не действует
        self.assertEqual(m["review"]["llm_calls"], 3 * m["review"]["checked"])
        kept = [f for f in rep["findings"] if f["kind"] == "contradiction"]
        self.assertEqual([{f["a"]["clause"], f["b"]["clause"]} for f in kept], [{"5.2", "9.4"}])
        self.assertEqual(kept[0]["confidence"], 1.0)
        self.assertEqual(len(kept[0]["votes"]), 3)
        self.assertTrue(rep["review_rejected"])
        self.assertTrue(all(f["id"].startswith("R") for f in rep["review_rejected"]))
        md = C.render_md(rep)
        self.assertIn("Отклонено перепроверкой", md)

    def test_check_kind_rules(self):
        self.assertEqual(C.check_kind(YES), "contradiction")
        self.assertEqual(C.check_kind(NO), "none")
        self.assertEqual(C.check_kind({**YES, "same_case": "unclear"}), "possible")
        self.assertEqual(C.check_kind({**YES, "explicit_exception": "yes"}), "none")

    def test_decide_majority(self):
        V = lambda *ks: [{"kind": k} for k in ks]  # noqa: E731
        self.assertEqual(C.decide(V("contradiction", "contradiction", "none")), ("contradiction", 0.67))
        self.assertEqual(C.decide(V("contradiction", "possible", "none")), ("possible", 0.67))
        self.assertEqual(C.decide(V("possible", "none", "none"))[0], None)

    def test_incremental_only_changed_doc(self):
        ws = corpus([win("tech", "5.2", SMETA_3), win("tech", "9.4", SMETA_5),
                     win("dbr", "5.2", SMETA_3), win("dbr", "9.4", SMETA_5)])
        llm = Stub(deadline_judge)
        rep = C.analyze(ws, DOCS, llm, workers=1, log=lambda *_: None, changed={"dbr"})
        self.assertTrue(rep["judged"])
        self.assertTrue(all("dbr" in (j["a"]["doc_id"], j["b"]["doc_id"]) for j in rep["judged"]))


if __name__ == "__main__":
    unittest.main()
