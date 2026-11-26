"""Нарезка на синтетическом DOCX (без данных корпуса)."""
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.chunking.clauses import build, find_external_refs
from app.parsers.docx_simple import parse

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def p(text, bold=False, lst=False):
    ppr = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="5"/></w:numPr></w:pPr>' if lst else ""
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f"<w:p>{ppr}<w:r>{rpr}<w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"


def make_docx(body):
    d = tempfile.mkdtemp()
    path = Path(d) / "Тест Дирекции ИБ.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f"<w:document {NS}><w:body>{body}</w:body></w:document>")
    return path


BODY = "".join([
    p("Тестовый регламент", bold=True),
    p("1. Первый раздел", bold=True),
    p("1.1. Пункт один, срок 3 рабочих дня."),
    p("элемент списка А", lst=True),
    p("a. подпункт буквой"),
    p("1.2. Пункт два по Регламенту «Поиск субподрядчика»."),
    p("1.2.1. Подпункт три."),
    p("2. Второй раздел", bold=True),
    p("2.1. Пункт раздела два."),
    "<w:tbl><w:tr><w:tc>" + p("Роль") + "</w:tc><w:tc>" + p("Срок") + "</w:tc></w:tr>"
    "<w:tr><w:tc>" + p("РП") + "</w:tc><w:tc>" + p("2 недели") + "</w:tc></w:tr></w:tbl>",
])

DOC = {"doc_id": "x", "title": "Тестовый регламент", "department": "IB", "stage": "project"}


class ChunkingTest(unittest.TestCase):
    def setUp(self):
        self.paras = parse(make_docx(BODY))
        self.ws = build(DOC, self.paras)
        self.by = {w.clause: w for w in self.ws}

    def test_parser_flags(self):
        self.assertTrue(self.paras[0].bold)
        self.assertTrue(any(x.is_list for x in self.paras))
        self.assertIn("РП | 2 недели", [x.text for x in self.paras])

    def test_clauses_and_paths(self):
        self.assertEqual(sorted(self.by), ["1", "1.1", "1.2", "1.2.1", "2", "2.1"])
        self.assertEqual(self.by["1"].kind, "section_title")
        self.assertIn("Раздел 1 «Первый раздел» › п. 1.1", self.by["1.1"].path)
        self.assertEqual(self.by["2.1"].section_title, "Второй раздел")

    def test_lists_attach_to_clause(self):
        t = self.by["1.1"].text
        self.assertIn("• элемент списка А", t)
        self.assertIn("a. подпункт буквой", t)

    def test_table_rows_attach_to_last_clause(self):
        self.assertIn("РП | 2 недели", self.by["2.1"].text)

    def test_numbers_kept_verbatim(self):
        self.assertTrue(self.by["1.1"].text.startswith("1.1. Пункт один, срок 3 рабочих дня."))

    def test_external_refs(self):
        self.assertEqual(self.by["1.2"].external_refs, ["Поиск субподрядчика"])
        self.assertEqual(find_external_refs("согласно Положению о закупках «Закупки»"), ["Закупки"])
        self.assertEqual(find_external_refs("Поиск идёт в соответствии с регламентом подбора субподрядчика."),
                         ["Регламентом подбора субподрядчика"])
        self.assertEqual(find_external_refs("согласно процедуре. Далее"), [])

    def test_ids_stable_and_unique(self):
        again = build(DOC, self.paras)
        self.assertEqual([w.window_id for w in self.ws], [w.window_id for w in again])
        self.assertEqual(len({w.window_id for w in self.ws}), len(self.ws))

    def test_long_clause_split_keeps_path(self):
        long = "".join([p("1. Раздел"), p("1.1. Начало.")] + [p("x" * 500, lst=True) for _ in range(6)])
        ws = build({**DOC, "title": "T"}, parse(make_docx(long)))
        parts = [w for w in ws if w.clause == "1.1"]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all("п. 1.1" in w.path for w in parts))
        self.assertTrue(all(len(w.text) <= 1800 + 520 for w in parts))


if __name__ == "__main__":
    unittest.main()
