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
    label: str = ""              # подпись для пользователя и модели: «п. 4.2» или «раздел «…» (часть 2)»
    section_path: list = field(default_factory=list)
    review: bool = False         # из страницы, требующей проверки (doc-intel)

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


def ref_label(w):
    """Как назвать место в документе: «п. 4.2» для пунктов, «раздел «…» (часть 2)» для окон по заголовкам."""
    get = w.get if isinstance(w, dict) else lambda k, d=None: getattr(w, k, d)
    if get("kind") == "heading_section" and get("label"):
        return get("label")
    return f"п. {get('clause')}"


def find_external_refs(text):
    refs = {m.group(1).strip() for m in REF_QUOTED.finditer(text)}
    for m in REF_NAMED.finditer(text):
        name = m.group(0).strip()
        refs.add(name[0].upper() + name[1:])
    return sorted(refs)


def _wid(doc_id, clause, part):
    return hashlib.sha1(f"{doc_id}:{clause}:{part}".encode()).hexdigest()[:12]


STRICT_NUM = re.compile(r"^\d+(?:\.\d+)*\.\s+\S")          # «4.2. текст» — номер с точкой после
WIN_MIN, WIN_MAX, TAIL_MIN, NOSEC_MAX = 600, 1000, 200, 2400   # окна раздела (решение 24.11: 600–1000), без раздела ≈ 2400
SENT = re.compile(r"(?<=[.!?;])\s+(?=[А-ЯЁA-Z«\d•])")
ABBR_END = re.compile(r"(?:\bп|\bпп|\bст|\bт\.\s?д|\bт\.\s?е|\bг|\bруб|\bгг|\bсм|\bрис|\bтабл|\b[А-ЯЁ])\.$")


def mode_of(paras):
    """Режим нарезки документа: «пункты» (нумерация в тексте, как в регламентах хакатона), «заголовки» или «без раздела»."""
    body = [p for p in paras if p.text.strip() and not p.is_list and not p.table]
    strict = sum(1 for p in body if STRICT_NUM.match(p.text))
    if strict >= 2 and strict >= 0.3 * max(1, len(body)):
        return "clauses"
    heads = sum(1 for p in paras if p.level or p.section_path)
    return "headings" if heads >= 1 else "plain"


def build(doc, paras):
    """doc: dict с doc_id, title, department, stage. paras: список Para из разборщика.
    Нумерованные документы — по пунктам (как было, id окон не меняются); остальные — по заголовкам или без раздела."""
    mode = mode_of(paras)
    if mode != "clauses":
        return build_sections(doc, paras, mode)
    return build_clauses(doc, paras)


def _sentences_of(text):
    parts, out = SENT.split(text), []
    for x in parts:                                      # не рвать после сокращений «п.», «т. д.», инициалов
        if out and ABBR_END.search(out[-1]):
            out[-1] += " " + x
        else:
            out.append(x)
    return [x for x in out if x.strip()]


def _pack(units, limit_min, limit_max):
    """Предложения/абзацы → окна limit_min…limit_max знаков, перекрытие — одно предложение, хвост < TAIL_MIN — к предыдущему."""
    wins, cur, size = [], [], 0
    for u in units:
        while len(u) > limit_max:                         # очень длинное предложение — по границе слова
            cut = u.rfind(" ", 0, limit_max)
            cut = cut if cut > limit_max // 2 else limit_max
            units_part, u = u[:cut], u[cut:].lstrip()
            if cur:
                wins.append(cur)
                cur, size = [], 0
            wins.append([units_part])
        if cur and size + len(u) > limit_max and size >= limit_min:
            wins.append(cur)
            cur, size = [cur[-1]], 0                       # перекрытие — последнее предложение, в «≥ min» не считается
        cur.append(u)
        size += len(u)
    if cur and (size >= TAIL_MIN or not wins):
        wins.append(cur)
    elif cur and wins:
        wins[-1] += cur[1:] if cur and wins[-1] and cur[0] == wins[-1][-1] else cur
    return [" ".join(w).strip() for w in wins if " ".join(w).strip()]


def build_sections(doc, paras, mode):
    """Окна внутри раздела (путь заголовков), 600–1000 знаков; без заголовков — до ≈ 2400 знаков по абзацам."""
    stack, groups = [], []                               # groups: (path_list, [paras])
    for p in paras:
        if p.section_path:
            path = [x for x in p.section_path if x]
        elif p.level and not p.table:
            stack = stack[:p.level - 1] + [p.text.strip()]
            path = list(stack)
            if groups and groups[-1][0] == path:
                continue
            groups.append((path, []))
            continue
        else:
            path = list(stack)
        if not groups or groups[-1][0] != path:
            groups.append((path, []))
        groups[-1][1].append(p)
    windows, top_no, last_top = [], 0, None
    for gi, (path, ps) in enumerate(groups):
        if not ps:
            continue
        if path[:1] != last_top:
            top_no, last_top = top_no + 1, path[:1]
        if mode == "plain" or not path:
            units = [("• " if p.is_list else "") + p.text for p in ps]
            chunks = _pack(units, WIN_MIN, NOSEC_MAX)
        else:
            units = []
            for p in ps:
                units += [("• " if p.is_list else "") + x for x in ([p.text] if p.table else _sentences_of(p.text))]
            chunks = _pack(units, WIN_MIN, WIN_MAX)
        title = path[-1] if path else doc["title"]
        for i, text in enumerate(chunks):
            key = f"§{gi + 1}" + (f".{i + 1}" if len(chunks) > 1 else "")
            lab = (f"раздел «{title}»" if path else "текст документа") + (f" (часть {i + 1})" if len(chunks) > 1 else "")
            windows.append(Window(
                window_id=hashlib.sha1(f"{doc['doc_id']}:{ps[0].idx}:{i}".encode()).hexdigest()[:12],
                doc_id=doc["doc_id"], department=doc["department"], stage=doc["stage"],
                section=str(top_no), section_title=title, clause=key,
                path=" › ".join([doc["title"]] + path), text=text, kind="heading_section", part=i,
                para_from=ps[0].idx, para_to=ps[-1].idx, external_refs=find_external_refs(text),
                label=lab, section_path=list(path), review=any(getattr(p, "review", False) for p in ps)))
    return windows


def build_clauses(doc, paras):
    """Нарезка по пунктам «N.M.» (регламенты хакатона) — без изменений, id окон стабильны."""
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
