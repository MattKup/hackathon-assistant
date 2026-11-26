"""Простой разборщик DOCX без зависимостей: абзацы по порядку с признаками списка и жирного.

Второй адаптер рядом с doc-intel (IMPL_PLAN §4): даёт тот же список абзацев,
на котором работает нарезка. Таблицы разворачиваются в строки «ячейка | ячейка».
"""
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass
class Para:
    idx: int
    text: str
    is_list: bool = False
    bold: bool = False
    table: bool = False
    level: int = 0               # уровень заголовка 1…6 (0 — обычный абзац); для нарезки по заголовкам
    section_path: list = None    # готовый путь раздела от источника (doc-intel sectionPath), иначе None
    page: int = 0
    review: bool = False         # страница требует проверки (doc-intel NEEDS_REVIEW/RESCUE)


def _text(el):
    parts = []
    for node in el.iter():
        if node.tag == W + "t" and node.text:
            parts.append(node.text)
        elif node.tag == W + "tab":
            parts.append("\t")
        elif node.tag in (W + "br", W + "cr"):
            parts.append("\n")
    return re.sub(r"[  ]+", " ", "".join(parts)).strip()


def _bold(p):
    runs = [r for r in p.iter(W + "r") if (r.find(W + "t") is not None and (r.find(W + "t").text or "").strip())]
    if not runs:
        return False
    def is_b(r):
        b = r.find(f"{W}rPr/{W}b")
        return b is not None and b.get(W + "val") not in ("0", "false")
    return all(is_b(r) for r in runs)


HEADING_NAME = re.compile(r"^(?:heading|заголовок)\s*(\d)$", re.I)
TITLE_NAME = re.compile(r"^(?:title|название|заглавие)$", re.I)
WORD_HEAD = re.compile(r"^(глава|раздел|статья|приложение)\s+[\dIVXLC]+\b", re.I)


def _styles(z):
    """styleId → (уровень заголовка или 0). Уровень: outlineLvl стиля, имя «heading N / Заголовок N / Title», цепочка basedOn."""
    try:
        root = ET.fromstring(z.read("word/styles.xml"))
    except KeyError:
        return {}
    raw = {}
    for st in root.iter(W + "style"):
        sid = st.get(W + "styleId")
        name = (st.find(W + "name").get(W + "val") if st.find(W + "name") is not None else "") or ""
        ol = st.find(f"{W}pPr/{W}outlineLvl")
        based = st.find(W + "basedOn")
        raw[sid] = (name, int(ol.get(W + "val")) if ol is not None and (ol.get(W + "val") or "").isdigit() else None,
                    based.get(W + "val") if based is not None else None)

    def level(sid, depth=0):
        if sid not in raw or depth > 5:
            return 0
        name, ol, based = raw[sid]
        if ol is not None and ol < 9:
            return ol + 1
        m = HEADING_NAME.match(name.strip())
        if m:
            return int(m.group(1))
        if TITLE_NAME.match(name.strip()):
            return 1
        return level(based, depth + 1) if based else 0
    return {sid: level(sid) for sid in raw}


def _heading_level(el, text, styles, bold):
    """Уровень заголовка абзаца: outlineLvl → стиль → «целиком жирный и короткий» → «Глава/Раздел/Статья N»."""
    ol = el.find(f"{W}pPr/{W}outlineLvl")
    if ol is not None and (ol.get(W + "val") or "").isdigit() and int(ol.get(W + "val")) < 9:
        return int(ol.get(W + "val")) + 1
    ps = el.find(f"{W}pPr/{W}pStyle")
    if ps is not None and styles.get(ps.get(W + "val"), 0):
        return styles[ps.get(W + "val")]
    short = len(text) <= 120 and len(text.split()) <= 16 and not text.rstrip().endswith((".", ";", ":", ","))
    if bold and short and el.find(f"{W}pPr/{W}numPr") is None:
        return 1
    if short and WORD_HEAD.match(text):
        return 1
    return 0


def parse(path):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
        styles = _styles(z)
    body = root.find(W + "body")
    out = []
    for el in body:
        if el.tag == W + "p":
            t = _text(el)
            if t:
                b = _bold(el)
                out.append(Para(len(out), t, el.find(f"{W}pPr/{W}numPr") is not None, b, level=_heading_level(el, t, styles, b)))
        elif el.tag == W + "tbl":
            for tr in el.iter(W + "tr"):
                cells = [_text(tc) for tc in tr.findall(W + "tc")]
                if any(cells):
                    out.append(Para(len(out), " | ".join(cells), table=True))
    return out
