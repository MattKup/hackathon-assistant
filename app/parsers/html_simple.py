"""Разборщик HTML / тела страницы Confluence (формат storage) без зависимостей → тот же список Para, что у DOCX.

h1…h6 → заголовки уровня 1…6; p, li (список), строки таблиц («ячейка | ячейка»). Макросы Confluence
(ac:structured-macro): текст из ac:rich-text-body берётся, параметры (ac:parameter) и ссылки ri:* — нет.
Абзац целиком жирный и короткий без других заголовков рядом — запасной заголовок уровня 1.
"""
import re
from html.parser import HTMLParser

from app.parsers.docx_simple import Para

BLOCK = {"p", "li", "div", "td", "th", "pre", "blockquote"}
SKIP = {"ac:parameter", "script", "style", "ac:plain-text-link-body"}


class _P(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.buf, self.level, self.is_list, self.bold_only = [], [], 0, False, True
        self.skip, self.row, self.in_table, self.strong = 0, None, 0, 0

    def flush(self):
        t = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        if t:
            short = len(t) <= 120 and len(t.split()) <= 16 and not t.endswith((".", ";", ":", ","))
            lvl = self.level or (1 if (self.bold_only and self.saw_strong and short and not self.is_list) else 0)
            self.out.append(Para(len(self.out), t, is_list=self.is_list, bold=self.bold_only and self.saw_strong, level=lvl))
        self.buf, self.level, self.is_list, self.bold_only, self.saw_strong = [], 0, False, True, False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
            return
        if tag in ("strong", "b"):
            self.strong += 1
            self.saw_strong = True
        if tag == "table":
            self.in_table += 1
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.buf = []
        elif re.fullmatch(r"h[1-6]", tag):
            self.flush()
            self.level = int(tag[1])
        elif tag in BLOCK:
            self.flush()
            self.is_list = tag == "li"
        elif tag == "br":
            self.buf.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag in ("strong", "b"):
            self.strong = max(0, self.strong - 1)
        if tag in ("td", "th") and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.buf)).strip())
            self.buf = []
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.out.append(Para(len(self.out), " | ".join(self.row), table=True))
            self.row = None
        elif tag == "table":
            self.in_table = max(0, self.in_table - 1)
        elif re.fullmatch(r"h[1-6]", tag) or (tag in BLOCK and self.row is None):
            self.flush()

    def handle_data(self, data):
        if self.skip:
            return
        if data.strip() and not self.strong and self.row is None:
            self.bold_only = False
        self.buf.append(data)


def parse_html(html, title=""):
    p = _P()
    p.saw_strong = False
    p.feed(html)
    p.flush()
    out = p.out
    if title:                                      # заголовок страницы Confluence — корень пути
        out = [Para(0, title.strip(), level=1)] + [Para(i + 1, x.text, x.is_list, x.bold, x.table, x.level + 1 if x.level else 0)
                                                   for i, x in enumerate(out)]
    return out
