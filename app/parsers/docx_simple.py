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


def parse(path):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    out = []
    for el in body:
        if el.tag == W + "p":
            t = _text(el)
            if t:
                out.append(Para(len(out), t, el.find(f"{W}pPr/{W}numPr") is not None, _bold(el)))
        elif el.tag == W + "tbl":
            for tr in el.iter(W + "tr"):
                cells = [_text(tc) for tc in tr.findall(W + "tc")]
                if any(cells):
                    out.append(Para(len(out), " | ".join(cells), table=True))
    return out
