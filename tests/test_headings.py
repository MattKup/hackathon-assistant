"""Нарезка по заголовкам (универсальная версия): документы без нумерации пунктов, Confluence, PDF через doc-intel."""
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.chunking.clauses import build, mode_of
from app.parsers.docx_simple import Para, parse
from app.parsers.html_simple import parse_html

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
DOC = {"doc_id": "d1", "title": "Регламент закупок", "department": "LAW", "stage": "full_cycle"}
LONG = ("Заявку на закупку создаёт инициатор в системе. Её проверяет юрист в течение двух рабочих дней. "
        "После проверки заявку согласует руководитель направления. ") * 6


def docx(paras_xml, styles_xml=""):
    f = Path(tempfile.mkdtemp()) / "t.docx"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("word/document.xml", f"<w:document {NS}><w:body>{paras_xml}</w:body></w:document>")
        if styles_xml:
            z.writestr("word/styles.xml", f"<w:styles {NS}>{styles_xml}</w:styles>")
    return f


def p(text, style=None, bold=False):
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f"<w:p>{ppr}<w:r>{rpr}<w:t>{text}</w:t></w:r></w:p>"


STYLES = ('<w:style w:styleId="1"><w:name w:val="heading 1"/></w:style>'
          '<w:style w:styleId="2"><w:name w:val="Заголовок 2"/></w:style>')


class HeadingsTest(unittest.TestCase):
    def test_styled_headings_two_levels_no_p0(self):
        f = docx(p("Общие положения", "1") + p("Короткий текст раздела.") + p("Согласование", "1") +
                 p("Сроки", "2") + p(LONG) + p(LONG), STYLES)
        paras = parse(f)
        self.assertEqual(mode_of(paras), "headings")
        ws = build(DOC, paras)
        self.assertTrue(all(w.kind == "heading_section" and w.clause != "0" for w in ws))
        deep = [w for w in ws if w.section_path == ["Согласование", "Сроки"]]
        self.assertGreaterEqual(len(deep), 2)                      # длинный раздел — несколько окон
        self.assertTrue(all(len(w.text) <= 1000 + 400 for w in deep))
        self.assertIn("Регламент закупок › Согласование › Сроки", deep[0].path)
        self.assertIn("раздел «Сроки»", deep[0].label)

    def test_bold_headings_heuristic(self):
        f = docx(p("Порядок согласования", bold=True) + p(LONG) + p("Это жирная фраза с точкой в конце.", bold=True) +
                 p("Ответственность", bold=True) + p("Нарушение сроков фиксирует служба качества."))
        ws = build(DOC, parse(f))
        titles = {w.section_title for w in ws}
        self.assertIn("Порядок согласования", titles)
        self.assertIn("Ответственность", titles)
        self.assertNotIn("Это жирная фраза с точкой в конце.", titles)

    def test_plain_document_windows_up_to_2400(self):
        f = docx(p(LONG) + p(LONG) + p(LONG))
        paras = parse(f)
        self.assertEqual(mode_of(paras), "plain")
        ws = build(DOC, paras)
        self.assertTrue(all(len(w.text) <= 2400 + 600 for w in ws))
        self.assertTrue(all(w.label.startswith("текст документа") for w in ws))

    def test_numbered_document_stays_clauses(self):
        paras = [Para(i, f"{i + 1}.1. Пункт номер {i}. Текст пункта.") for i in range(8)]
        self.assertEqual(mode_of(paras), "clauses")

    def test_number_like_sentences_not_sections(self):
        paras = [Para(0, "Закупки", level=1), Para(1, "3 рабочих дня даётся на проверку заявки."),
                 Para(2, "2026 год: лимит закупок утверждает директор.")]
        ws = build(DOC, paras)
        self.assertEqual({w.section_title for w in ws}, {"Закупки"})

    def test_confluence_html(self):
        h = ('<h1>Согласование договора</h1><p>Договор согласует юрист за два рабочих дня.</p>'
             '<ac:structured-macro ac:name="info"><ac:parameter ac:name="title">X</ac:parameter>'
             '<ac:rich-text-body><p>Без подписи работы не начинать.</p></ac:rich-text-body></ac:structured-macro>'
             '<h2>Сроки</h2><p>Срок — 2 рабочих дня.</p>')
        ws = build(DOC, parse_html(h, "Регламент закупок (wiki)"))
        self.assertTrue(any(w.section_path[-1] == "Сроки" for w in ws))
        self.assertTrue(any("Без подписи работы не начинать" in w.text for w in ws))
        self.assertFalse(any(" X " in f" {w.text} " for w in ws))   # параметр макроса не попал в текст

    def test_ir_section_path(self):
        paras = [Para(0, "Текст раздела на первой странице.", section_path=["Закупки", "Сроки"], page=1),
                 Para(1, "Продолжение раздела на второй странице.", section_path=["Закупки", "Сроки"], page=2),
                 Para(2, "Отдельный раздел.", section_path=["Закупки", "Ответственность"], page=2, review=True)]
        ws = build(DOC, paras)
        self.assertEqual(len([w for w in ws if w.section_path == ["Закупки", "Сроки"]]), 1)   # раздел переходит страницу
        self.assertTrue(any(w.review for w in ws))


if __name__ == "__main__":
    unittest.main()
