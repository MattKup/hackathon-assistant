"""Ответ по найденным окнам + проверки кодом.

Проверки (без второго вызова LLM): ссылка ref существует; quote — дословная подстрока окна
(после нормализации пробелов/кавычек); шаг опирается на окно своего подразделения;
числа шага есть в цитате окна. Шаг, не прошедший проверку, убирается и попадает в `rejected`.
Если после проверки не осталось опоры — verdict понижается до no_answer.
"""
import json
import re

from app.chunking.clauses import ref_label
from pathlib import Path

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
SYSTEM = (PROMPTS / "system_answer.md").read_text()
SCHEMA = json.loads((PROMPTS / "answer.schema.json").read_text())
NUM = re.compile(r"\d+(?:[.,]\d+)?")


def norm(s):
    s = s.lower().replace("ё", "е").replace("«", '"').replace("»", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip(" .;:")


def build_user(question, windows, dept_label):
    parts = []
    for i, w in enumerate(windows, 1):
        where = ref_label(w) + ("" if w.get("kind") == "heading_section" else f" · раздел «{w['section_title']}»")
        parts.append(f"[w{i}] {w['department_name']} · {w['doc_title']} · {where}\n{w['text']}")
    return f"Фрагменты регламентов:\n\n" + "\n\n".join(parts) + f"\n\nПодразделение: {dept_label}\n\nВопрос: {question}"


def _sentences(text):
    return [s for s in re.split(r"(?<=[.;:!?])\s+|\n+", text) if s.strip()]


def repair_quote(quote, text, min_overlap=0.8):
    """Цитата почти дословная → вернуть предложение источника, где совпадает ≥ 80 % слов цитаты."""
    qt = [w for w in re.findall(r"\w+", norm(quote)) if len(w) > 2]
    if not qt:
        return None
    best, score = None, 0.0
    for s in _sentences(text):
        st = set(re.findall(r"\w+", norm(s)))
        sc = sum(1 for w in qt if w in st) / len(qt)
        if sc > score:
            best, score = s.strip(), sc
    return best if score >= min_overlap else None


ROOT = Path(__file__).resolve().parents[2]
GARBLED = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")   # иероглифы/кана/хангыль — сбой генерации (v7 q026)


def _entities():
    """Роли и сокращения для проверки «роль из шага есть в пункте» (универсально: из конфигурации, не из кода)."""
    abbr, roles, groups = [], [], {}
    try:
        syn = json.loads((ROOT / "config" / "synonyms.json").read_text())
        for k, v in (syn.get("abbreviations") or {}).items():
            names = [x.strip() for x in k.split(",") if x.strip()]
            exp = (v.get("expansions") if isinstance(v, dict) else v) or []
            abbr += names
            for n in names + list(exp):            # «РП» = «Руководитель проекта»: одно и то же
                groups.setdefault(n, set()).update(names + list(exp))
    except (OSError, ValueError):
        pass
    try:
        roles = json.loads((ROOT / "config" / "documents.json").read_text()).get("roles") or []
    except (OSError, ValueError):
        pass
    return abbr, roles, groups


ABBR, ROLES, GROUPS = _entities()


def _has_abbr(a, text):
    return re.search(r"(?<![\wА-Яа-яЁё])" + re.escape(a) + r"(?![\wА-Яа-яЁё])", text) is not None


def _role_re(role):
    """Фраза роли подряд, по основам слов: «Руководитель проекта» ≠ «Руководитель отдела управления проектами»."""
    words = [w for w in re.findall(r"[а-яё]+", norm(role)) if len(w) > 2]
    return re.compile(r"(?<![а-яё])" + r"[а-яё]*(?:[\s-]+[а-яё]{1,2})?[\s-]+".join(re.escape(w[:5]) for w in words) + r"[а-яё]*") if words else None


def _has_role(role, text):
    r = _role_re(role)
    return bool(r and r.search(norm(text)))


PERSON = re.compile(r"руковод|менеджер|архитект|директор|специалист|администратор|инженер|аналитик|логист|владел|координатор|"
                    r"сметчик|юрист|бухгалт|sales|manager|engineer", re.I)


def _present(name, text):
    for n in GROUPS.get(name, {name}) | {name}:
        if (_has_abbr(n, text) if n.isupper() or len(n) <= 4 else _has_role(n, text)):
            return True
    return False


def foreign_entities(step_text, window_text, question=""):
    """Роли/сокращения, которые модель назвала в шаге, а в пункте (и в вопросе) их нет — с учётом расшифровок
    («РП» = «Руководитель проекта»). v7: «Руководитель отдела УП», «Менеджер по качеству», ТКП вместо КП.
    Длинные названия проверяются первыми и вырезаются, чтобы «Архитектор» не срабатывал внутри «Пресейл-архитектор»."""
    names = sorted(set(ABBR) | set(ROLES) | {n for g in GROUPS.values() for n in g if n in ABBR or PERSON.search(n)},
                   key=len, reverse=True)       # длинные названия — только роли людей (не «Заявка на пресейл», v8 q017)
    rest, out = step_text, []
    for n in names:
        short = n.isupper() or len(n) <= 4
        found = _has_abbr(n, rest) if short else _has_role(n, rest)
        if not found:
            continue
        if not _present(n, window_text) and not _present(n, question):
            out.append(n)
        if short:
            rest = re.sub(r"(?<![\wА-Яа-яЁё])" + re.escape(n) + r"(?![\wА-Яа-яЁё])", " ", rest)
        else:
            r = _role_re(n)
            rest = r.sub(" ", norm(rest)) if r else rest
    return out


STOP = {"можно", "нужно", "какой", "какие", "какая", "каким", "когда", "сколько", "который", "проект", "проекта",
        "работы", "работ", "заказчика", "договора", "дирекции", "дирекция", "регламент", "регламенте", "если"}


def stems(text):
    return {w[:4] for w in re.findall(r"[а-яё]{5,}", norm(text)) if w not in STOP}


def _colloquial_map():
    """Разговорные/английские синонимы из словаря: основы слова вопроса → термин регламента («проджект менеджер» → РП)."""
    out = []
    try:
        syn = json.loads((ROOT / "config" / "synonyms.json").read_text())
        for k, v in (syn.get("colloquial") or {}).items():
            term = v.get("term") if isinstance(v, dict) else v
            out.append((stems(k), term or ""))
    except (OSError, ValueError):
        pass
    return out


def _term_in(term, text):
    abbr = re.findall(r"\b[A-ZА-ЯЁ]{2,}\b", term)
    return bool(stems(term) & stems(text)) or any(_has_abbr(a, text) for a in abbr)


_COLLOQ = None


def added_from_question(step_text, window_text, question):
    """Слова вопроса, которые модель перенесла в шаг, хотя в пункте их нет (26.11: «проверьте наличие
    гарантийного письма (п. 8.3)» — в п. 8.3 о письме ни слова). ≥ 2 таких основ — шаг дополнен из вопроса."""
    global _COLLOQ
    if _COLLOQ is None:
        _COLLOQ = _colloquial_map()
    # синоним из вопроса не считается добавкой, только если его термин есть в пункте (ревью Fable: «проджект менеджер» при «РП»);
    # «гарантийка» при пункте без гарантийного письма — по-прежнему добавка (кейс 1)
    ok = set()
    for st, term in _COLLOQ:
        if _term_in(term, window_text):
            ok |= st
    return sorted((stems(step_text) & stems(question)) - stems(window_text) - ok)


PERMIT = re.compile(r"^\s*(да|можно|можете|допускается|разрешено)\b|(?<!не\s)\b(можете|можно|допускается|разрешается)\b", re.I)
EXCEPT = re.compile(r"за\s+исключением\s+([^),.;]+)|кроме\s+([^),.;]+)|исключени\w*\s+(?:является|—|-)?\s*([^),.;]+)", re.I)


def _ban_applies(summary, ban):
    """Запрет по той же теме (≥ 2 общих основ) и не снят исключением, которое ответ называет («за исключением RFI»)."""
    if len(stems(summary) & stems(ban)) < 2:
        return False
    for m in EXCEPT.finditer(ban):
        exc = next(g for g in m.groups() if g)
        if stems(exc) & stems(summary) or any(w in summary for w in re.findall(r"\b[A-ZА-ЯЁ]{2,}\b", exc)):
            return False
    return True
BAN = re.compile(r"запрещ|не\s+допуска|не\s+может\b|не\s+могут\b|не\s+вправе|нельзя|не\s+разреша", re.I)


REPLACEABLE = {"entity_not_in_source", "added_from_question", "number_not_in_source"}


def check(raw, windows, question=""):
    q = question.split("\n\n")[0]
    out = {**raw, "steps": [], "rejected": []}
    for st in raw.get("steps", []):
        m = re.fullmatch(r"w(\d+)", st.get("ref", ""))
        reason = None
        if not m or not (1 <= int(m.group(1)) <= len(windows)):
            reason = "ref_missing"
        else:
            w = windows[int(m.group(1)) - 1]
            if st.get("quote") and norm(st["quote"]) not in norm(w["text"]):
                fixed = repair_quote(st["quote"], w["text"])
                if fixed:
                    st = {**st, "quote": fixed, "quote_repaired": True}
            if not st.get("quote") or norm(st["quote"]) not in norm(w["text"]):
                reason = "quote_not_verbatim"
            else:
                nums_step = set(NUM.findall(st.get("text", "") + " " + st.get("deadline", "")))
                if nums_step - set(NUM.findall(w["text"])):
                    reason = "number_not_in_source"
                elif q and w.get("kind") != "practice" and len(added_from_question(st.get("text", ""), w["text"], q)) >= 2:
                    reason = "added_from_question"
                elif GARBLED.search(" ".join(str(st.get(k, "")) for k in ("text", "who", "deadline"))):
                    reason = "garbled"
                elif w.get("kind") != "practice" and foreign_entities(st.get("text", ""), w["text"], q):
                    reason = "entity_not_in_source"
                if not reason and st.get("who") and w.get("kind") != "practice" and foreign_entities(st["who"], w["text"], q):
                    st = {**st, "who": ""}        # роль только в поле «кто» и не из пункта — не показываем
        if reason in REPLACEABLE and st.get("quote"):
            # текст шага с добавкой, а цитата дословная → показываем цитату пункта вместо пересказа (судья v8: q012 — ответ
            # был верным, но шаг с выдуманной ролью отбрасывался целиком и ответ уходил в «нет ответа»)
            out["rejected"].append({**st, "reason": reason, "replaced_by_quote": True})
            st = {**st, "text": st["quote"].rstrip(".") + ".", "who": "", "deadline": st.get("deadline", "") if reason != "number_not_in_source" else "",
                  "replaced": reason}
            reason = None
        if reason:
            out["rejected"].append({**st, "reason": reason})
        else:
            w = windows[int(st["ref"][1:]) - 1]
            out["steps"].append({**st, "window_id": w["window_id"], "clause": w["clause"],
                                 "department": w["department"], "doc_title": w["doc_title"],
                                 "source_text": w["text"], "section": w.get("section_title", ""),
                                 "external_refs": w.get("external_refs", []), "label": ref_label(w)})
    # числа в кратком ответе и оговорке — только из процитированных пунктов (ревью Fable: q054, q082, q115, q118)
    cited = " ".join(st["source_text"] for st in out["steps"])
    if out["steps"]:
        src = set(NUM.findall(cited))
        if set(NUM.findall(out.get("summary", ""))) - src or foreign_entities(out.get("summary", ""), cited, q):
            out["summary_replaced"] = out.get("summary", "")
            out["summary"] = " ".join(st["text"] for st in out["steps"][:2])
        if out.get("caveat") and set(NUM.findall(out["caveat"])) - src:
            out["caveat"] = " ".join(x for x in _sentences(out["caveat"]) if not (set(NUM.findall(x)) - src))
    # разрешение в ответе при запрете в процитированном пункте → оговорка с цитатой запрета (судья: q145 «Да, можете…»)
    if out["steps"] and out.get("verdict") == "answer" and PERMIT.search(out.get("summary", "")):
        bans = [(st.get("label") or f"п. {st['clause']}", x) for st in out["steps"] for x in _sentences(st["source_text"]) if BAN.search(x) and _ban_applies(out["summary"], x)]
        if bans:
            clause, ban = bans[0]
            out["verdict"] = "answer_with_caveat"
            out["summary_replaced"] = out.get("summary", "")
            # разрешение модели не показываем: сотрудник читает краткий ответ первым (судья v9: q145 «Да, можете…» осталось сверху)
            out["summary"] = f"Однозначного разрешения в регламенте нет. По {clause}: «{ban.strip()[:260]}»"
            out["caveat"] = ((out.get("caveat") or "") + " Проверьте, не относится ли этот запрет к вашему случаю; при сомнении — уточните у службы качества.").strip()
            out["permit_vs_ban"] = True
    if raw.get("verdict") in ("answer", "answer_with_caveat", "conflict") and raw.get("steps") and not out["steps"]:
        out["verdict"] = "no_answer"
        out["downgraded"] = "все шаги без проверенной опоры"
    return out


def answer(llm, question, windows, dept_label):
    raw, meta = llm.chat_json(SYSTEM, build_user(question, windows, dept_label), SCHEMA)
    return check(raw, windows, question), meta
