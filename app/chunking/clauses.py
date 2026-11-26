"""Нарезка регламента по пунктам.

Структура корпуса (26.11): номера набраны в тексте — «N. Заголовок раздела»,
«N.M. текст пункта», «N.M.K. подпункт»; маркированные списки и «a./b.» относятся
к предыдущему пункту. Окно = один пункт с его списками; у окна есть путь
«Раздел N «Заголовок» › п. N.M». Длинный пункт делится по абзацам с тем же путём.
"""
import hashlib
import re
from dataclasses import dataclass, field, asdict

NUM = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.*)$", re.S)
MAX_CHARS = 1800


@dataclass
class Window:
    window_id: str
    doc_id: str
    department: str
    stage: str
    section: str
    section_title: str
    clause: str
    path: str
    text: str
    kind: str = "clause"
    part: int = 0
    para_from: int = 0
    para_to: int = 0
    external_refs: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


# Отсылки к документам вне базы. В корпусе они без кавычек: «согласно регламенту
# подбора субподрядчика», «в регламенте контроля поставок в проектах»; реже — «…».
_DOC = r"(?:[Рр]егламент\w*|[Пп]оложени\w*|[Ии]нструкци\w*|[Мм]етодик\w*|[Пп]олитик\w*|[Пп]роцедур\w*)"
REF_QUOTED = re.compile(_DOC + r"[^«»\n.;]{0,60}?«([^»]{3,150})»")
_NAMED_DOC = r"(?:[Рр]егламент\w*|[Пп]оложени\w*|[Ии]нструкци\w*|[Мм]етодик\w*|[Пп]олитик\w*)"  # «процедура» — процесс, не документ
REF_NAMED = re.compile(r"(?:внутренн\w+\s+)?" + _NAMED_DOC +
                       r"\s+((?:подбора|контроля|привлечения|согласования|управления|закупок|выбора|оценки|проверки)"
                       r"\b[^.,;:()\n«»]{3,80})")


def find_external_refs(text):
    refs = {m.group(1).strip() for m in REF_QUOTED.finditer(text)}
    for m in REF_NAMED.finditer(text):
        name = m.group(0).strip()
        refs.add(name[0].upper() + name[1:])
    return sorted(refs)


def _wid(doc_id, clause, part):
    return hashlib.sha1(f"{doc_id}:{clause}:{part}".encode()).hexdigest()[:12]


def build(doc, paras):
    """doc: dict с doc_id, title, department, stage. paras: список Para из разборщика."""
    groups = []          # (section, section_title, clause, [paras])
    section, section_title = "0", doc["title"]
    cur = None
    for p in paras[1:] if paras and paras[0].text.strip() == doc["title"].strip() else paras:
        m = NUM.match(p.text) if not p.table else None
        if m and not p.is_list:
            num, rest = m.group(1), m.group(2).strip()
            if "." not in num:                       # заголовок раздела «N. …»
                section, section_title = num, rest
                cur = (section, section_title, num, [p])
                groups.append(cur)
                continue
            cur = (section, section_title, num, [p])
            groups.append(cur)
            continue
        if cur is None:
            cur = (section, section_title, section, [])
            groups.append(cur)
        cur[3].append(p)

    windows = []
    for sec, sec_title, clause, ps in groups:
        if not ps:
            continue
        is_header = clause == sec and len(ps) == 1
        path = f"{doc['title']} › Раздел {sec} «{sec_title}»" + ("" if clause == sec else f" › п. {clause}")
        chunks, buf = [], []
        for p in ps:
            line = ("• " if p.is_list else "") + p.text
            if buf and sum(len(x) for x, _ in buf) + len(line) > MAX_CHARS:
                chunks.append(buf)
                buf = []
            buf.append((line, p.idx))
        if buf:
            chunks.append(buf)
        for i, ch in enumerate(chunks):
            text = "\n".join(x for x, _ in ch)
            windows.append(Window(
                window_id=_wid(doc["doc_id"], clause, i), doc_id=doc["doc_id"],
                department=doc["department"], stage=doc["stage"],
                section=sec, section_title=sec_title, clause=clause, path=path, text=text,
                kind="section_title" if is_header else "clause", part=i,
                para_from=ch[0][1], para_to=ch[-1][1], external_refs=find_external_refs(text)))
    return windows
