"""Поиск противоречий и несостыковок между пунктами регламентов (офлайн, stdlib).

Конвейер:
  1. Кандидаты пар без O(n²) по LLM: для каждого окна k ближайших соседей по теме (tf-idf по
     инвертированному индексу; на стенде — плюс косинус эмбеддингов bge-m3) отдельно в каждой
     «корзине» отношения: тот же документ / та же дирекция (документы, действующие одновременно,
     напр. ИБ пресейл ↔ ИБ проект) / общая политика ↔ регламент / разные дирекции.
  2. Сигналы кодом: общие роли и действия + расхождение сроков, противоположные модальности
     (запрещено ↔ допускается/обязан), разные статусы для одного объекта, разные ответственные
     за назначение, разные исключения, условие ↔ безусловное правило. Пары ранжируются по
     скору (тема + сигналы), фильтр отсекает пары без сигналов и с низкой близостью.
  3. Судья — локальная LLM с JSON-схемой: kind ∈ {contradiction, possible, dependency,
     dept_difference, none}, ситуация, объяснение, дословные цитаты из обоих пунктов.
     Бюджет вызовов ограничен (по умолчанию 300 пар; доля на пары разных дирекций — cross_share).
  4. Проверки кодом: цитаты дословные (norm / repair_quote, допускается перестановка A↔B),
     числа объяснения есть в текстах пунктов; находка без опоры отбрасывается в `rejected`.
     Отношение пар задаёт допустимые виды: у разных дирекций противоречий нет (→ dept_difference).
  5. Зависимости кодом (без LLM): отсылки к внешним документам и к другим пунктам («п. 8.4»).

Выход: report (dict) → conflicts.json, conflicts.md (для службы качества), metrics.
"""
import json
import math
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from app.answer.core import norm, repair_quote
from app.search.bm25 import tokens

KINDS = ("contradiction", "possible", "dependency", "dept_difference", "none")
GENERAL_DEPTS = {"ALL", "COMMON", "GENERAL", "CORP"}     # общая политика компании: действует вместе с любым регламентом
STAGE_RU = {"presale": "пресейл", "project": "проект", "full_cycle": "пресейл и проект"}
KIND_RU = {"contradiction": "Противоречие", "possible": "Возможное противоречие / неоднозначность",
           "dependency": "Зависимость от условия", "dept_difference": "Различие между дирекциями"}
REL_RU = {"same_doc": "оба пункта из одного документа",
          "same_dept": "пункты из разных документов одной дирекции, действующих одновременно (напр. пресейл и проект)",
          "general": "пункт общей политики и пункт регламента подразделения — действуют одновременно",
          "cross_dept": "пункты из регламентов РАЗНЫХ подразделений — у каждой дирекции свои правила"}

# ---------------------------------------------------------------- сигналы
ROLE_PATTERNS = [
    ("РП", r"\bРП\b|[Рр]уководител\w*\s+проект(?!н)\w*"),
    ("SS", r"\bSS\b"), ("TPS", r"\bTPS\b"), ("TSS", r"\bTSS\b"), ("BDM", r"\bBDM\b"),
    ("АМ", r"\bАМ\b|[Аа]ккаунт-менеджер\w*"),
    ("ПА", r"\bПА\b|[Пп]ресейл-аналитик\w*"),
    ("ГИП", r"\bГИП\b|[Гг]лавн\w*\s+инженер\w*"),
    ("ЦК", r"\bЦК\b"),
    ("Архитектор", r"[Аа]рхитектор\w*"),
    ("Ресурсный менеджер", r"[Рр]есурсн\w*\s+менеджер\w*|\bРМ\b"),
    ("Менеджер по продаже решений", r"[Мм]енеджер\w*\s+по\s+продаж\w*"),
    ("Руководитель ОУП", r"[Рр]уководител\w*\s+отдела\s+управления\s+проект\w*"),
    ("Руководитель дирекции", r"[Рр]уководител\w*\s+(?:\w+\s+)?(?:[Дд]ирекци|[Дд]епартамент|[Нн]аправлени)\w*"),
    ("Директор", r"[Дд]иректор\w*"),
    ("Владелец бюджета", r"[Вв]ладел\w*\s+бюджет\w*"),
    ("Заказчик", r"[Зз]аказчик\w*"),
    ("Субподрядчик", r"[Сс]убподрядчик\w*"),
]
ROLES = [(n, re.compile(p)) for n, p in ROLE_PATTERNS]
ACTIONS = {k: re.compile(p, re.I) for k, p in {
    "согласование": r"согласов|согласу", "создание": r"созда[её]?|заводит|заводится|заведен",
    "назначение": r"назнач|выделени\w* (?:РП|ресурс|архитект|команд|исполнит|сотрудник)|выделя\w* (?:РП|ресурс|архитект|команд)|распредел", "смена статуса": r"статус",
    "закрытие": r"закры", "привлечение": r"привлек|привлеч", "подписание": r"подпис",
    "утверждение": r"утвержд|утверд", "старт работ": r"старт|начал\w* работ|приступ",
    "эскалация": r"эскал", "отклонение": r"отклон", "оплата": r"оплат", "передача": r"переда[её]т|передач|направля",
    "приоритет": r"приоритет", "архив": r"архив"}.items()}
OBJECTS = {k: re.compile(p, re.I) for k, p in {
    "договор": r"договор", "смета": r"смет", "заявка": r"заявк", "карточка": r"карточк", "CRM": r"\bCRM\b",
    "ТКП": r"\bТКП\b", "акт": r"\bакт\w*\b", "бюджет": r"бюджет", "расчёт": r"расч[её]т", "SLA": r"\bSLA\b",
    "система управления проектами": r"систем\w* управления проект", "конкурс": r"конкурс", "анкетирование": r"анкет"}.items()}
DEADLINE = re.compile(r"(\d+)\s*(?:-?х\s+)?(рабоч\w*\s+(?:дн\w*|день|час\w*)|р\.\s?д\.?|рд\b|календарн\w*\s+(?:дн\w*|день)"
                      r"|дн(?:я|ей|ем|ям)\b|день\b|час\w*|недел\w*|месяц\w*|минут\w*)", re.I)
PROHIB = re.compile(r"запрещ|не\s+допуска|недопустим|нельзя|не\s+мо(?:жет|гут)|не\s+долж|не\s+вправе|только\s+при|"
                    r"возможно\s+только|не\s+меня|не\s+изменя|не\s+заводит|не\s+выполня|не\s+начина|не\s+принима", re.I)
PERMIT = re.compile(r"допускается|разрешается|\bможет\b|\bмогут\b|вправе|исключени|кроме\s+случа|\bвозможно\b", re.I)
OBLIG = re.compile(r"обязательн|\bдолж(?:ен|на|ны)\b|необходимо|требуется|услови\w+\s+(?:\w+\s+){0,4}явля", re.I)
EXCEPT = re.compile(r"исключени|кроме\s+случа", re.I)
COND = re.compile(r"в\s+некоторых\s+случаях|при\s+необходимости|при\s+наличии|\bесли\b|в\s+случае", re.I)
RESP = re.compile(r"назнач|отвеча|ответствен|выделени\w* (?:РП|ресурс|архитект|команд)|выделя\w* (?:РП|ресурс|архитект|команд)|распредел", re.I)
STATUS = re.compile(r"стату\w*[^«\n.]{0,30}«([^»]{2,60})»", re.I)
AFTER = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?(?:После|По\s+завершении|По\s+окончании)\s+(\w+\s+\w+)", re.I)
CLAUSE_REF = re.compile(r"п(?:ункт\w*|\.)\s*(\d+(?:\.\d+)+)")
NUM = re.compile(r"\d+(?:[.,]\d+)?")
UNIT_DAYS = [("час", 1 / 8), ("минут", 1 / 480), ("недел", 5), ("месяц", 21)]

WEIGHTS = {"deadline_diff": 2.0, "modality_opposite": 1.5, "status_diff": 1.5, "resp_diff": 1.5,
           "exception_diff": 1.0, "obligation_vs_prohibition": 1.0, "status_vs_action": 1.0, "precondition_diff": 1.0, "condition_vs_rule": 0.7}
SIGNAL_RU = {"deadline_diff": "разные сроки при общем действии", "modality_opposite": "запрет ↔ разрешение/исключение",
             "status_diff": "разные статусы одного объекта", "resp_diff": "разные роли при назначении/ответственности",
             "exception_diff": "оба пункта задают исключения", "obligation_vs_prohibition": "обязанность ↔ запрет",
             "status_vs_action": "статус объекта ↔ действие закрытия/смены статуса",
             "precondition_diff": "одно действие после разных событий",
             "condition_vs_rule": "условие в одном пункте, безусловное правило в другом"}


def _deadlines(text):
    out = set()
    for m in DEADLINE.finditer(text):
        v, u = float(m.group(1)), m.group(2).lower()
        days = v
        for key, mult in UNIT_DAYS:
            if key in u:
                days = v * mult
                break
        out.add((round(days, 3), m.group(0).strip()))
    return out


def sentences(text):
    """Предложения пункта; строки маркированного списка приклеиваются к вводной фразе («…являются:» + пункты)."""
    lines = []
    for ln in text.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        if lines and (re.match(r"^(?:[•·\-–—*]|[a-zа-я]\.|\d+\))\s", ln) or lines[-1].endswith(":")):
            lines[-1] += " " + ln
        else:
            lines.append(ln)
    out = []
    for ln in lines:
        out += [s for s in re.split(r"(?<=[^\d\s][.;!?])\s+(?=[А-ЯЁA-Z«])", ln) if s.strip()]
    return out


def _sent_feats(s):
    no_prohib = PROHIB.sub(" ", s)
    roles = {n for n, rx in ROLES if rx.search(s)}
    acts = {n for n, rx in ACTIONS.items() if rx.search(s)}
    objs = {n for n, rx in OBJECTS.items() if rx.search(s)}
    # ключ действия: (действие, участник/объект) — конкретный; (действие, "*") — общий (весит вдвое меньше)
    keys = {(a, x) for a in acts for x in roles | objs} | {(a, "*") for a in acts}
    m = AFTER.match(s)
    trig = frozenset(tokens(m.group(1))) if m else None
    return {"trigger": trig, "roles": roles, "actions": acts, "objects": objs, "keys": keys,
            "deadlines": _deadlines(s), "prohib": bool(PROHIB.search(s)), "permit": bool(PERMIT.search(no_prohib)),
            "oblig": bool(OBLIG.search(s)), "except": bool(EXCEPT.search(s)), "cond": bool(COND.search(s)),
            "resp": bool(RESP.search(s)), "statuses": {norm(x) for x in STATUS.findall(s)}}


def features(text):
    """Признаки пункта. Модальность, сроки и ответственность привязаны к ключам действий своего предложения,
    чтобы длинный пункт со списком ролей не «совпадал» со всем подряд."""
    ss = [_sent_feats(s) for s in sentences(text)]
    f = {"roles": set(), "actions": set(), "objects": set(), "statuses": set(), "except": False,
         "prohib": set(), "permit": set(), "oblig": set(), "cond": set(), "rule": set(),
         "resp": set(), "resp_roles": set(), "dl": defaultdict(set), "trig": defaultdict(set), "chars": len(text)}
    for x in ss:
        for k in ("roles", "actions", "objects", "statuses"):
            f[k] |= x[k]
        f["except"] |= x["except"]
        for k in ("prohib", "permit", "oblig"):
            if x[k]:
                f[k] |= x["keys"]
        f["cond" if x["cond"] else "rule"].update(x["keys"])
        if x["resp"]:
            f["resp"] |= x["keys"]
            f["resp_roles"] |= x["roles"] - {"Заказчик"}
        for key in x["keys"]:
            f["dl"][key] |= x["deadlines"]
            if x["trigger"] and key[1] != "*":
                f["trig"][key].add(x["trigger"])
    return f


RARE_ACTIONS = set()   # заполняет set_rare(): редкое действие весит как конкретный ключ («приоритет», «эскалация»)


def set_rare(feats, share=0.04):
    df = Counter(a for f in feats for a in f["actions"])
    RARE_ACTIONS.clear()
    RARE_ACTIONS.update(a for a, c in df.items() if c <= max(2, share * len(feats)))


def _match(ka, kb):
    """Общие ключи действий и вес совпадения: конкретный (или редкое действие) — 1.0, только общий — 0.5."""
    common = ka & kb
    spec = {k for k in common if k[1] != "*" or k[0] in RARE_ACTIONS}
    return (spec or common), (1.0 if spec else 0.5 if common else 0.0)


def _fmt(keys):
    return ", ".join(sorted(a if x == "*" else f"{a}: {x}" for a, x in keys)[:4])


def signals(fa, fb):
    """Сигналы возможной несостыковки для пары пунктов: {имя: (описание, вес совпадения)}."""
    roles, acts, objs = fa["roles"] & fb["roles"], fa["actions"] & fb["actions"], fa["objects"] & fb["objects"]
    s = {}
    best = None
    for key in fa["dl"].keys() & fb["dl"].keys():
        da, db = fa["dl"][key], fb["dl"][key]
        if da and db and not ({d for d, _ in da} & {d for d, _ in db}):
            w = 1.0 if key[1] != "*" else 0.5
            if best is None or w > best[0]:
                best = (w, f"{_fmt({key})}: A — " + ", ".join(sorted(t for _, t in da)) + "; B — " + ", ".join(sorted(t for _, t in db)))
    if best:
        s["deadline_diff"] = (best[1], best[0])
    mo = [_match(fa["prohib"], fb["permit"]), _match(fb["prohib"], fa["permit"])]
    mo = max(mo, key=lambda x: x[1])
    ob = max([_match(fa["prohib"], fb["oblig"]), _match(fb["prohib"], fa["oblig"])], key=lambda x: x[1])
    if mo[1]:
        s["modality_opposite"] = (_fmt(mo[0]), mo[1])
    elif ob[1] == 1.0:
        s["obligation_vs_prohibition"] = (_fmt(ob[0]), ob[1])
    if fa["statuses"] and fb["statuses"] and objs & {"карточка", "CRM", "заявка"} and not (fa["statuses"] & fb["statuses"]):
        s["status_diff"] = ("A: " + ", ".join(sorted(fa["statuses"])) + "; B: " + ", ".join(sorted(fb["statuses"])), 1.0)
    elif objs & {"карточка", "CRM", "заявка"}:
        for x, y in ((fa, fb), (fb, fa)):
            if x["statuses"] and ("закрытие" in y["actions"] or "смена статуса" in y["actions"]) and not (x["statuses"] & y["statuses"]):
                s["status_vs_action"] = ("статус: " + ", ".join(sorted(x["statuses"])) + "; в другом пункте — закрытие/смена статуса", 1.0)
                break
    rk, rw = _match(fa["resp"], fb["resp"])
    if rw == 1.0 and (fa["resp_roles"] ^ fb["resp_roles"]):
        s["resp_diff"] = (_fmt(rk) + "; разные роли: " + ", ".join(sorted(fa["resp_roles"] ^ fb["resp_roles"])), 1.0)
    for key in fa["trig"].keys() & fb["trig"].keys():
        ta, tb = fa["trig"][key], fb["trig"][key]
        if not any(len(x & y) / max(1, len(x | y)) >= 0.5 for x in ta for y in tb):
            s["precondition_diff"] = (_fmt({key}) + ": разные «после …»", 1.0)
            break
    if fa["except"] and fb["except"] and (acts or objs):
        s["exception_diff"] = ("исключения в обоих пунктах", 1.0)
    cr = max([_match(fa["cond"], fb["rule"] - fb["cond"]), _match(fb["cond"], fa["rule"] - fa["cond"])], key=lambda x: x[1])
    if cr[1] == 1.0:
        s["condition_vs_rule"] = (_fmt(cr[0]), 1.0)
    return s, {"roles": sorted(roles), "actions": sorted(acts), "objects": sorted(objs)}


# ---------------------------------------------------------------- кандидаты
class TopicIndex:
    """tf-idf по инвертированному индексу: соседи окна без перебора всех пар (масштаб — десятки тысяч окон)."""

    def __init__(self, windows, max_df=0.25, query_terms=30):
        toks = [tokens(w.get("section_title", "") + "\n" + w["text"]) for w in windows]
        n = len(toks)
        df = Counter()
        for t in toks:
            df.update(set(t))
        idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}
        self.vec, self.top = [], []
        self.post = defaultdict(list)
        for i, tk in enumerate(toks):
            v = {t: (1 + math.log(c)) * idf[t] for t, c in Counter(tk).items()}
            z = math.sqrt(sum(x * x for x in v.values())) or 1.0
            v = {t: x / z for t, x in v.items()}
            self.vec.append(v)
            for t, x in v.items():
                if df[t] <= max(2, max_df * n):
                    self.post[t].append((i, x))
            self.top.append(sorted(v.items(), key=lambda kv: -kv[1])[:query_terms])

    def cos(self, i, j):
        a, b = self.vec[i], self.vec[j]
        if len(a) > len(b):
            a, b = b, a
        return sum(x * b.get(t, 0.0) for t, x in a.items())

    def scores(self, i):
        acc = defaultdict(float)
        for t, x in self.top[i]:
            for j, y in self.post.get(t, ()):
                if j != i:
                    acc[j] += x * y
        return acc


def relation(wa, wb):
    if wa["doc_id"] == wb["doc_id"]:
        return "same_doc"
    if wa["department"] == wb["department"]:
        return "same_dept"
    if wa["department"] in GENERAL_DEPTS or wb["department"] in GENERAL_DEPTS:
        return "general"
    return "cross_dept"


def _dense_matrix(vectors):
    n = len(vectors)
    m = [[0.0] * n for _ in range(n)]
    for i in range(n):
        vi = vectors[i]
        for j in range(i + 1, n):
            m[i][j] = m[j][i] = sum(x * y for x, y in zip(vi, vectors[j]))
    return m


def candidates(windows, k=8, k_cross=3, vectors=None, dense_max=4000, block=True, m_block=10):
    """Пары (i, j) с отношением и близостью: k соседей в каждой корзине отношения (tf-idf ∪ эмбеддинги)."""
    idx = TopicIndex(windows)
    dense = _dense_matrix(vectors) if vectors is not None and len(windows) <= dense_max else None
    pairs = {}
    for i, wi in enumerate(windows):
        buckets = defaultdict(list)
        for j, s in idx.scores(i).items():
            buckets[relation(wi, windows[j])].append((s, j))
        if dense is not None:
            dbuck = defaultdict(list)
            for j in range(len(windows)):
                if j != i:
                    dbuck[relation(wi, windows[j])].append((dense[i][j], j))
        for rel in ("same_doc", "same_dept", "general", "cross_dept"):
            kk = k_cross if rel == "cross_dept" else k
            near = {j for _, j in sorted(buckets.get(rel, []), reverse=True)[:kk]}
            if dense is not None:
                near |= {j for _, j in sorted(dbuck.get(rel, []), reverse=True)[:kk]}
            for j in near:
                a, b = (i, j) if i < j else (j, i)
                if (a, b) in pairs:
                    continue
                wa, wb = windows[a], windows[b]
                if wa["doc_id"] == wb["doc_id"] and wa["clause"] == wb["clause"]:
                    continue                                    # части одного пункта
                if norm(wa["text"]) == norm(wb["text"]):
                    continue
                sim = idx.cos(a, b)
                dcos = dense[a][b] if dense is not None else None
                if dcos is not None:
                    sim = 0.5 * sim + 0.5 * max(0.0, (dcos - 0.5) / 0.5)
                pairs[(a, b)] = {"a": a, "b": b, "relation": rel, "sim": round(sim, 4),
                                 "dense": round(dcos, 4) if dcos is not None else None, "via": "topic"}
    if block:
        _block_by_action(windows, idx, pairs, dense, m=m_block)
    return list(pairs.values())


STRONG = {"deadline_diff", "modality_opposite", "status_diff", "resp_diff", "exception_diff",
          "obligation_vs_prohibition", "status_vs_action", "precondition_diff"}


def _block_by_action(windows, idx, pairs, dense, m=10):
    """Второй канал кандидатов: пункты про одно действие часто написаны разными словами (низкий tf-idf).
    Блок = общее действие (согласование, назначение, закрытие…) внутри документа/дирекции/с общей политикой;
    из блока для окна берём ≤ m партнёров с сильным сигналом. Цена — квадрат размера блока внутри дирекции,
    а не всей базы."""
    feats = [features(w["text"]) for w in windows]
    set_rare(feats)
    post = defaultdict(list)
    for i, f in enumerate(feats):
        for a in f["actions"]:
            post[a].append(i)
    n = len(windows)
    idf = {a: math.log((n + 1) / (len(v) + 0.5)) for a, v in post.items()}
    for i, wi in enumerate(windows):
        if is_heading(wi):
            continue
        cand = defaultdict(float)
        for a in feats[i]["actions"]:
            for j in post[a]:
                if j != i and relation(wi, windows[j]) != "cross_dept" and not is_heading(windows[j]):
                    cand[j] += idf[a]
        best = []
        for j, w_act in cand.items():
            a, b = (i, j) if i < j else (j, i)
            if (a, b) in pairs:
                continue
            sig, shared = signals(feats[a], feats[b])
            strong = {k for k, (_, w) in sig.items() if w == 1.0} & STRONG
            if not strong:
                continue
            best.append((sum(WEIGHTS[k] * w for k, (_, w) in sig.items()) + 0.3 * w_act + 0.4 * len(shared["roles"]), a, b))
        for _, a, b in sorted(best, reverse=True)[:m]:
            wa, wb = windows[a], windows[b]
            if (wa["doc_id"] == wb["doc_id"] and wa["clause"] == wb["clause"]) or norm(wa["text"]) == norm(wb["text"]):
                continue
            sim = idx.cos(a, b)
            dcos = dense[a][b] if dense is not None else None
            if dcos is not None:
                sim = 0.5 * sim + 0.5 * max(0.0, (dcos - 0.5) / 0.5)
            pairs[(a, b)] = {"a": a, "b": b, "relation": relation(wa, wb), "sim": round(sim, 4),
                             "dense": round(dcos, 4) if dcos is not None else None, "via": "action_block"}


def is_heading(w):
    return w.get("kind") == "section_title" and len(w["text"]) < 160


def score_pairs(windows, pairs, sim_pass=0.45, sim_cond=0.25, sim_min=0.06):
    """Скор = близость темы + сигналы (с поправкой на длину: в длинном пункте совпадения случайнее)."""
    feats = [features(w["text"]) for w in windows]
    set_rare(feats)
    out = []
    for p in pairs:
        wa, wb = windows[p["a"]], windows[p["b"]]
        if is_heading(wa) or is_heading(wb):
            continue
        fa, fb = feats[p["a"]], feats[p["b"]]
        sig, shared = signals(fa, fb)
        topic = 0.3 * min(2, len(shared["roles"])) + 0.3 * min(2, len(shared["actions"])) + 0.2 * min(2, len(shared["objects"]))
        same_sec = 0.3 if wa["doc_id"] == wb["doc_id"] and wa.get("section") == wb.get("section") else 0.0
        length = 1 / (1 + max(0, max(fa["chars"], fb["chars"]) - 500) / 1000)
        sw = sum(WEIGHTS[k] * w for k, (_, w) in sig.items())
        # сигнал важен, только если пункты про одно и то же: вес сигналов растёт с близостью темы
        score = length * sw * (0.4 + 2 * min(p["sim"], 0.5)) + 2 * p["sim"] + topic + same_sec + (0.5 if p["relation"] != "cross_dept" else 0.0)
        strong = {k for k, (_, w) in sig.items() if w == 1.0} - {"condition_vs_rule"}
        sig = {k: v for k, (v, _) in sig.items()}
        if p["relation"] == "cross_dept":
            passed = bool(strong & {"deadline_diff", "resp_diff", "status_diff", "modality_opposite"}) and p["sim"] >= sim_min
        else:
            passed = (bool(strong) and p["sim"] >= sim_min) or ("condition_vs_rule" in sig and p["sim"] >= sim_cond) or p["sim"] >= sim_pass
        out.append({**p, "signals": sig, "shared": shared, "score": round(score, 3), "passed": passed})
    out.sort(key=lambda x: -x["score"])
    return out


def select(scored, budget=300, cross_share=0.2, per_window=6):
    """Бюджет вызовов LLM: сначала пары, где противоречие возможно; доля — на различия дирекций.
    Одно окно — не больше per_window раз (длинные «пункты-списки» иначе забирают бюджет)."""
    def take(pool, n, used):
        out = []
        for p in pool:
            if len(out) >= n:
                break
            if per_window and (used[p["a"]] >= per_window or used[p["b"]] >= per_window):
                continue
            used[p["a"]] += 1
            used[p["b"]] += 1
            out.append(p)
        return out
    inn = [p for p in scored if p["passed"] and p["relation"] != "cross_dept"]
    crs = [p for p in scored if p["passed"] and p["relation"] == "cross_dept"]
    if budget is None:                 # ночной режим: все пары после фильтра
        budget = len(inn) + len(crs)
        cross_share = len(crs) / max(1, budget)
    n_cross = min(len(crs), int(round(budget * cross_share)))
    used = Counter()
    got_in = take(inn, budget - n_cross, used)
    got_cross = take(crs, budget - len(got_in), Counter())
    chosen = got_in + got_cross
    for n, p in enumerate(chosen, 1):
        p["order"] = n                 # место в очереди судьи: метрики «что нашлось бы при меньшем бюджете»
    return chosen


# ---------------------------------------------------------------- судья
SYSTEM = """Ты — аудитор внутренних регламентов компании. Тебе даны два пункта регламентов: A и B.
Определи, согласуются ли они. Опирайся только на текст этих пунктов, ничего не додумывай и не добавляй от себя.

Виды (поле kind):
- contradiction — оба пункта относятся к одной и той же ситуации, шагу и роли, но дают несовместимые указания:
  разные сроки на одно и то же действие; разные ответственные за одно и то же действие; в одном запрещено,
  в другом допускается без оговорённого исключения; несовместимый порядок шагов или статусов.
  Выполнить оба пункта одновременно нельзя.
- possible — указания выглядят несовместимыми, но могут относиться к разным объектам, этапам или случаям,
  а текст этого не уточняет; нужно решение службы качества.
- dependency — пункты не спорят, но один выполним только при условии, заданном в другом пункте или в другом документе.
- dept_difference — пункты из регламентов разных подразделений задают разные правила для похожего действия
  (это законное различие, не противоречие).
- none — пункты про разные действия или объекты; дополняют или уточняют друг друга; общее правило и явно
  оговорённое исключение из него; шаги, которые просто идут друг за другом.

НЕ противоречие (ставь none):
  - требования, которые можно выполнить вместе (два условия для одного действия — это оба условия сразу);
  - один пункт перечисляет больше или меньше участников, чем другой, со словами «может привлекать»;
  - пункты про разные этапы (пресейл / реализация), разные объекты (заявка / проект / смета) или разные случаи,
    если это видно из текста;
  - кто ищет/организует и кто выполняет работу — разные роли, а не два ответственных за одно действие.
Будь строг: contradiction ставь только при явной несовместимости одного и того же действия. Сомневаешься между
contradiction и possible — ставь possible. Сомневаешься между possible и none — ставь none.

situation — общая ситуация или действие, о котором оба пункта (коротко).
quote_a — дословный фрагмент пункта A (скопируй символ в символ, одно предложение или его часть), quote_b — из пункта B.
explanation — 1–2 коротких предложения (до 300 знаков): в чём несовместимость или почему это не противоречие.
Для kind=none цитаты можно оставить пустыми."""

_SOFT_RULE = ("Будь строг: contradiction ставь только при явной несовместимости одного и того же действия. Сомневаешься между\n"
              "contradiction и possible — ставь possible. Сомневаешься между possible и none — ставь none.")
# Этап 1 ночного режима — мягкий судья (полнота): без списка «не противоречие» (прогон 1: 5/7 ориентиров)
SYSTEM_SOFT = SYSTEM[:SYSTEM.index("НЕ противоречие (ставь none):")] + _SOFT_RULE + SYSTEM[SYSTEM.index("\n\nsituation —"):]

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["situation", "quote_a", "quote_b", "explanation", "kind"],
          "properties": {"situation": {"type": "string"}, "quote_a": {"type": "string"},
                         "quote_b": {"type": "string"}, "explanation": {"type": "string"},
                         "kind": {"type": "string", "enum": list(KINDS)}}}


def _head(w):
    return (f"{w.get('department_name', w['department'])} · «{w.get('doc_title', '')}» "
            f"({STAGE_RU.get(w['stage'], w['stage'])}) · п. {w['clause']} · раздел «{w.get('section_title', '')}»")


def build_user(wa, wb, rel, sig):
    hint = "; ".join(f"{SIGNAL_RU[k]} ({v})" for k, v in sig.items()) or "нет"
    return (f"Отношение пунктов: {REL_RU[rel]}.\n"
            f"Совпадения, замеченные кодом (это лишь совпадения слов, сами по себе не противоречие): {hint}.\n\n"
            f"Пункт A — {_head(wa)}\n{wa['text']}\n\nПункт B — {_head(wb)}\n{wb['text']}\n\n"
            f"Сравни пункты A и B и верни JSON.")


def verify_quote(q, text):
    """Дословная цитата (после нормализации) или исправленное предложение источника; иначе None."""
    if not q or len([t for t in re.findall(r"\w+", norm(q)) if len(t) > 2]) < 2:
        return None, False
    if norm(q) in norm(text):
        return q.strip(), False
    frags = [f for f in re.split(r"\s*(?:\.\.\.|…)\s*", q) if f.strip()]
    if len(frags) > 1:                                   # цитата с пропуском: каждый кусок дословно и по порядку
        nt, pos = norm(text), 0
        for f in frags:
            i = nt.find(norm(f), pos)
            if i < 0 or not re.search(r"\w", f):
                break
            pos = i + len(norm(f))
        else:
            if len(re.findall(r"\w+", " ".join(frags))) >= 3:
                return q.strip(), False
    fixed = repair_quote(q, text)
    if fixed and norm(fixed) in norm(text):
        return fixed, True
    return None, False


def _numbers_ok(expl, wa, wb):
    t = CLAUSE_REF.sub(" ", expl)
    for c in (wa["clause"], wb["clause"]):
        t = t.replace(c, " ")
    have = set(NUM.findall(wa["text"] + " " + wb["text"] + " " + wa["clause"] + " " + wb["clause"]))
    return not (set(NUM.findall(t)) - have)


def judge_pair(llm, wa, wb, p, max_tokens=900, system=None):
    raw, meta = llm.chat_json(system or SYSTEM, build_user(wa, wb, p["relation"], p["signals"]), SCHEMA, max_tokens=max_tokens)
    return raw, meta


CHECK_SYSTEM = """Ты — ревизор службы качества. Проверь гипотезу о несостыковке двух пунктов регламента (A и B).
Опирайся только на текст пунктов; соседние пункты (если даны) — лишь контекст для понимания.
Сначала коротко рассуди (reason), затем ответь на вопросы:
- same_action: оба пункта задают правило для ОДНОГО И ТОГО ЖЕ действия (одна роль или один объект), а не для разных шагов?
  yes / no / unclear
- same_case: правила относятся к одному этапу и случаю — или текст НЕ разделяет этапы/случаи?
  yes — один случай или текст их не разделяет; no — текст явно называет разные этапы, объекты или условия
- both_possible: можно ли в этой ситуации выполнить оба указания одновременно? yes / no / unclear
- explicit_exception: один пункт прямо оговорён как исключение из другого (словами «исключение», «кроме»,
  ссылкой на тот пункт или его условие)? yes / no
Два условия для одного действия выполнимы вместе (both_possible=yes). Разные списки «может привлекать» — тоже.
«Кто организует» и «кто выполняет» — разные действия (same_action=no)."""

_YNU = {"type": "string", "enum": ["yes", "no", "unclear"]}
CHECK_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": ["reason", "same_action", "same_case", "both_possible", "explicit_exception"],
                "properties": {"reason": {"type": "string"}, "same_action": _YNU, "same_case": _YNU,
                               "both_possible": _YNU, "explicit_exception": {"type": "string", "enum": ["yes", "no"]}}}


def check_kind(r):
    """Вид по ответам чек-листа (решает код, не модель)."""
    if r.get("same_action") == "no" or r.get("same_case") == "no" or r.get("both_possible") == "yes" \
            or r.get("explicit_exception") == "yes":
        return "none"
    if r.get("same_action") == "yes" and r.get("same_case") == "yes" and r.get("both_possible") == "no":
        return "contradiction"
    return "possible"


def _neighbors(windows, i, n=600):
    out = []
    for j in (i - 1, i + 1):
        if 0 <= j < len(windows) and windows[j]["doc_id"] == windows[i]["doc_id"]:
            out.append(f"п. {windows[j]['clause']}: {windows[j]['text'][:n]}")
    return out


def _verify_user(wa, wb, rel, situation="", ctx_a=(), ctx_b=()):
    ctx = ""
    if ctx_a or ctx_b:
        ctx = ("\n\nКонтекст — соседние пункты (только для понимания):\n"
               + "\n".join([f"рядом с A — {c}" for c in ctx_a] + [f"рядом с B — {c}" for c in ctx_b]))
    hyp = f"Гипотеза: несостыковка в ситуации «{situation}».\n" if situation else ""
    return (f"{hyp}Отношение пунктов: {REL_RU[rel]}.\n\nПункт A — {_head(wa)}\n{wa['text']}\n\n"
            f"Пункт B — {_head(wb)}\n{wb['text']}{ctx}\n\nПерепроверь пункты A и B и верни JSON.")


VOTERS = (("check_ab", False, False),
          ("check_ba", True, False),            # порядок B↔A: модель склонна к первому пункту
          ("check_ctx", False, True))           # с соседними пунктами: условие часто стоит в соседнем пункте


def vote(llm, windows, ia, ib, rel, voter, situation="", max_tokens=700):
    name, swap, ctx = voter
    wa, wb = windows[ia], windows[ib]
    ca, cb = (_neighbors(windows, ia), _neighbors(windows, ib)) if ctx else ((), ())
    user = _verify_user(wb, wa, rel, situation, cb, ca) if swap else _verify_user(wa, wb, rel, situation, ca, cb)
    raw, meta = llm.chat_json(CHECK_SYSTEM, user, CHECK_SCHEMA, max_tokens=max_tokens)
    return {"voter": name, "kind": check_kind(raw), "reason": (raw.get("reason") or "").strip(),
            "answers": {k: raw.get(k) for k in ("same_action", "same_case", "both_possible", "explicit_exception")}}, meta


def decide(votes):
    """Большинство из трёх перепроверок: ≥ 2 «есть несовместимость» — находка выжила; вид — contradiction,
    если так считают ≥ 2, иначе possible. Уверенность — доля голосов за итоговый вид."""
    ks = [v["kind"] for v in votes]
    n = len(ks)
    bad = sum(k in ("contradiction", "possible") for k in ks)
    contra = ks.count("contradiction")
    if n == 0 or bad * 2 <= n:
        return None, round((n - bad) / max(1, n), 2)
    kind = "contradiction" if contra * 2 > n else "possible"
    agree = contra if kind == "contradiction" else bad
    return kind, round(agree / n, 2)


def offline_judge(wa, wb, p):
    """Режим без стенда: кандидаты по сигналам кода, вид не выше «possible» (решает человек/LLM)."""
    strong = set(p["signals"]) - {"condition_vs_rule"}
    if not strong:
        return {"kind": "none", "situation": "", "quote_a": "", "quote_b": "", "explanation": ""}
    acts = [ACTIONS[a] for a in p["shared"]["actions"]] or [re.compile(".")]

    def pick(text):
        for snt in sentences(text):
            if any(rx.search(snt) for rx in acts):
                return snt
        return sentences(text)[0] if sentences(text) else ""
    return {"kind": "possible", "situation": ", ".join(p["shared"]["actions"]),
            "quote_a": pick(wa["text"]), "quote_b": pick(wb["text"]),
            "explanation": "Без LLM: " + "; ".join(SIGNAL_RU[k] for k in sorted(strong)) + "."}


def postprocess(raw, wa, wb, p):
    """Вид по отношению пар + проверка цитат и чисел. Возвращает (finding | None, rejected | None)."""
    kind = raw.get("kind") if raw.get("kind") in KINDS else "none"
    llm_kind, note = kind, None
    rel = p["relation"]
    if rel == "cross_dept" and kind in ("contradiction", "possible"):
        kind, note = "dept_difference", "разные дирекции: противоречие невозможно по определению → различие"
    elif rel in ("same_doc", "same_dept") and kind == "dept_difference":
        kind, note = "none", "одна дирекция: «различие дирекций» неприменимо"
    elif rel == "general" and kind == "dept_difference":
        kind, note = "possible", "общая политика ↔ регламент: различие требует решения службы качества"
    if kind == "none":
        return None, None
    qa, ra = verify_quote(raw.get("quote_a", ""), wa["text"])
    qb, rb = verify_quote(raw.get("quote_b", ""), wb["text"])
    if not (qa and qb):                                     # модель могла перепутать A и B
        sa, rsa = verify_quote(raw.get("quote_b", ""), wa["text"])
        sb, rsb = verify_quote(raw.get("quote_a", ""), wb["text"])
        if sa and sb:
            qa, ra, qb, rb = sa, rsa, sb, rsb
    base = {"kind": kind, "llm_kind": llm_kind, "relation": rel, "order": p.get("order"), "score": p["score"], "sim": p["sim"],
            "signals": p["signals"], "situation": raw.get("situation", "").strip(),
            "explanation": raw.get("explanation", "").strip()}
    if note:
        base["reclassified"] = note
    if not (qa and qb):
        return None, {**base, "reason": "quote_not_verbatim", "a": _ref(wa), "b": _ref(wb),
                      "quote_a": raw.get("quote_a", ""), "quote_b": raw.get("quote_b", "")}
    if not _numbers_ok(base["explanation"] + " " + base["situation"], wa, wb):
        return None, {**base, "reason": "number_not_in_source", "a": _ref(wa), "b": _ref(wb)}
    return {**base, "a": {**_ref(wa), "quote": qa, "quote_repaired": ra},
            "b": {**_ref(wb), "quote": qb, "quote_repaired": rb}}, None


def _ref(w):
    return {"window_id": w["window_id"], "doc_id": w["doc_id"], "doc_title": w.get("doc_title", ""),
            "department": w["department"], "stage": w["stage"], "clause": w["clause"],
            "section_title": w.get("section_title", "")}


# ---------------------------------------------------------------- зависимости кодом
def _sentence_with(text, needle):
    nn = norm(needle)
    for s in sentences(text):
        if nn in norm(s):
            return s.strip()
    return None


def dependencies(windows):
    by_clause = {(w["doc_id"], w["clause"]) for w in windows}
    out = []
    for w in windows:
        for ref in w.get("external_refs", []):
            q = _sentence_with(w["text"], ref) or (ref if norm(ref) in norm(w["text"]) else None)
            if q:
                out.append({"kind": "dependency_external", "target": ref, "a": {**_ref(w), "quote": q}})
        for m in CLAUSE_REF.finditer(w["text"]):
            c = m.group(1)
            if c != w["clause"] and (w["doc_id"], c) in by_clause:
                q = _sentence_with(w["text"], m.group(0))
                if q:
                    out.append({"kind": "dependency_internal", "target": "п. " + c, "a": {**_ref(w), "quote": q}})
    seen, uniq = set(), []
    for d in out:
        key = (d["a"]["window_id"], d["target"])
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


# ---------------------------------------------------------------- прогон
def enrich(windows, docs):
    by = {d["doc_id"]: d for d in docs}
    return [{**w, "doc_title": w.get("doc_title") or by.get(w["doc_id"], {}).get("display_title", ""),
             "department_name": w.get("department_name") or by.get(w["doc_id"], {}).get("department_name", w["department"])}
            for w in windows]


NIGHT = {"sim_pass": 0.4, "sim_cond": 0.15, "sim_min": 0.0, "m_block": 25, "per_window": None}


def _pmap(fn, items, workers):
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        yield from ex.map(fn, items)


def analyze(windows, docs, llm, budget=300, k=8, k_cross=3, cross_share=0.2, vectors=None, workers=4, log=print,
            night=False, changed=None):
    """night=True — ночной режим «точность выше стоимости»: без бюджета, шире фильтр, мягкий судья (этап 1)
    + три независимые перепроверки каждой находки contradiction/possible (этап 2), итог — по большинству.
    changed — множество doc_id: инкрементальный режим, только пары, где есть пункт изменённого документа."""
    t0 = time.time()
    windows = enrich(windows, docs)
    pairs = candidates(windows, k=k, k_cross=k_cross, vectors=vectors, m_block=NIGHT["m_block"] if night else 10)
    t_cand = time.time() - t0
    flt = {x: NIGHT[x] for x in ("sim_pass", "sim_cond", "sim_min")} if night else {}
    scored = score_pairs(windows, pairs, **flt)
    if changed is not None:
        scored = [p for p in scored if windows[p["a"]]["doc_id"] in changed or windows[p["b"]]["doc_id"] in changed]
    chosen = select(scored, budget=None if night else budget, cross_share=cross_share,
                    per_window=NIGHT["per_window"] if night else 6)
    log(f"окон={len(windows)} пар-кандидатов={len(pairs)} после фильтра={sum(p['passed'] for p in scored)} "
        f"к LLM={len(chosen)} режим={'ночь' if night else 'бюджет'} ({time.time() - t0:.1f} с)")
    findings, rejected, errors, judged = [], [], [], []
    tok = Counter()
    t1 = time.time()
    system1 = SYSTEM_SOFT if night else SYSTEM

    def one(job):
        p, swap = job
        wa, wb = windows[p["a"]], windows[p["b"]]
        try:
            if llm is None:
                return p, offline_judge(wa, wb, p), {}, None
            if swap:                                  # второй проход ночью: порядок B, A — потом цитаты обратно
                raw, meta = judge_pair(llm, wb, wa, p, system=system1)
                raw = {**raw, "quote_a": raw.get("quote_b", ""), "quote_b": raw.get("quote_a", ""), "pass": "ba"}
            else:
                raw, meta = judge_pair(llm, wa, wb, p, system=system1)
            return p, raw, meta, None
        except Exception as ex:                       # сбой одной пары не останавливает прогон
            return p, None, {}, str(ex)[:200]

    jobs1 = [(p, False) for p in chosen] + ([(p, True) for p in chosen] if night and llm is not None else [])
    for n, (p, raw, meta, err) in enumerate(_pmap(one, jobs1, workers), 1):
        tok["prompt"] += meta.get("prompt_tokens") or 0
        tok["completion"] += meta.get("completion_tokens") or 0
        wa, wb = windows[p["a"]], windows[p["b"]]
        if err:
            errors.append({"stage": 1, "a": _ref(wa), "b": _ref(wb), "error": err})
            continue
        judged.append({"order": p["order"], "pass": raw.get("pass", "ab"), "a": _ref(wa), "b": _ref(wb),
                       "relation": p["relation"], "score": p["score"], "signals": p["signals"],
                       "llm_kind": raw.get("kind"), "explanation": raw.get("explanation", "")})
        f, r = postprocess(raw, wa, wb, p)
        if f:
            f["_ab"] = (p["a"], p["b"])
            findings.append(f)
        if r:
            rejected.append(r)
        if n % 50 == 0:
            log(f"  этап 1: {n}/{len(jobs1)} находок={len(findings)} отброшено={len(rejected)} ошибок={len(errors)}")
    t_llm = time.time() - t1
    # два прохода этапа 1 → одна находка на пару: сильнейший вид, оба вердикта в stage1_votes
    rank = {"contradiction": 0, "possible": 1, "dependency": 2, "dept_difference": 3}
    best = {}
    for f in findings:
        key = f["_ab"]
        if key not in best or rank[f["kind"]] < rank[best[key]["kind"]]:
            best[key] = {**f, "stage1_votes": best.get(key, {}).get("stage1_votes", [])}
        best[key]["stage1_votes"] = best[key].get("stage1_votes", []) + [f["llm_kind"]]
    findings = list(best.values())

    # этап 2: перепроверка (только ночью и только с LLM)
    review_rejected, tok2, t2 = [], Counter(), time.time()
    to_check = [f for f in findings if night and llm is not None and f["kind"] in ("contradiction", "possible")]
    jobs = [(f, v) for f in to_check for v in VOTERS]

    def vjob(fv):
        f, v = fv
        try:
            res, meta = vote(llm, windows, f["_ab"][0], f["_ab"][1], f["relation"], v, f.get("situation", ""))
            return f, res, meta, None
        except Exception as ex:
            return f, None, {}, str(ex)[:200]

    for f, res, meta, err in _pmap(vjob, jobs, workers):
        tok2["prompt"] += meta.get("prompt_tokens") or 0
        tok2["completion"] += meta.get("completion_tokens") or 0
        if err:
            errors.append({"stage": 2, "a": f["a"], "b": f["b"], "error": err})
            continue
        f.setdefault("votes", []).append(res)
    survived, check_ids, n_stage1 = [], {id(f) for f in to_check}, len(findings)
    for f in findings:
        if id(f) in check_ids:
            f["stage1_kind"] = f["kind"]
            kind, conf = decide(f.get("votes", []))
            if kind is None:
                review_rejected.append({**f, "confidence_none": conf})
                continue
            f["kind"], f["confidence"] = kind, conf
        survived.append(f)
    if to_check:
        log(f"  этап 2: перепроверено {len(to_check)}, выжило {len(to_check) - len(review_rejected)}, "
            f"вызовов {len(jobs)} ({time.time() - t2:.0f} с)")
    t_rev = time.time() - t2
    findings = survived
    for f in findings + review_rejected:
        f.pop("_ab", None)
    seen, uniq = set(), []
    for f in sorted(findings, key=lambda f: (list(KIND_RU).index(f["kind"]), -f.get("confidence", 0), -f["score"])):
        key = tuple(sorted([(f["a"]["doc_id"], f["a"]["clause"]), (f["b"]["doc_id"], f["b"]["clause"])]))
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    for i, f in enumerate(uniq, 1):
        f["id"] = f"F{i:03d}"
    # не подтверждённое перепроверкой — на разбор СК по приоритету: сигналы кода + согласие двух проходов этапа 1.
    # Прогон 4: перепроверка qwen38 отклонила 5 из 7 ориентиров, а этот порядок ставит 6 из 7 в первые 18 из 60.
    for f in review_rejected:
        f["priority"] = round(f["score"] + sum(k in ("contradiction", "possible") for k in f.get("stage1_votes", [])), 3)
    review_rejected.sort(key=lambda f: -f["priority"])
    for i, f in enumerate(review_rejected, 1):
        f["id"] = f"R{i:03d}"
    deps = dependencies(windows)
    rel_c = Counter(p["relation"] for p in scored)
    passed_c = Counter(p["relation"] for p in scored if p["passed"])
    calls = len(chosen)
    m = {"docs": len(docs), "doc_ids": sorted(d["doc_id"] for d in docs), "windows": len(windows), "k": k,
         "k_cross": k_cross, "budget": None if night else budget, "night": night,
         "changed_docs": sorted(changed) if changed is not None else None,
         "dense": vectors is not None, "candidate_pairs": len(pairs), "candidates_by_relation": dict(rel_c),
         "passed_filter": sum(passed_c.values()), "passed_by_relation": dict(passed_c),
         "llm_calls": calls, "llm_errors": len(errors), "prompt_tokens": tok["prompt"],
         "completion_tokens": tok["completion"],
         "tokens_per_call": {"prompt": round(tok["prompt"] / max(1, calls)), "completion": round(tok["completion"] / max(1, calls))},
         "llm_kind_raw": dict(Counter(j["llm_kind"] for j in judged)),
         "stage1_findings": n_stage1,
         "review": {"checked": len(to_check), "survived": len(to_check) - len(review_rejected),
                    "rejected": len(review_rejected), "llm_calls": len(jobs), "prompt_tokens": tok2["prompt"],
                    "completion_tokens": tok2["completion"], "seconds": round(t_rev, 1),
                    "confidence": dict(Counter(str(f.get("confidence")) for f in uniq if "confidence" in f))},
         "findings_by_kind": dict(Counter(f["kind"] for f in uniq)),
         "findings_by_kind_at_budget": {str(n): dict(Counter(f["kind"] for f in uniq if f["order"] <= n))
                                        for n in (100, 200, 300, 500) if n < calls},
         "mode": "llm" if llm is not None else "offline", "rejected": len(rejected),
         "rejected_by_reason": dict(Counter(r["reason"] for r in rejected)),
         "dependencies_code": dict(Counter(d["kind"] for d in deps)),
         "seconds_candidates": round(t_cand, 1), "seconds_llm": round(t_llm, 1), "seconds_review": round(t_rev, 1),
         "seconds_total": round(time.time() - t0, 1), "workers": workers,
         "llm_calls_total": calls + len(jobs), "prompt_tokens_total": tok["prompt"] + tok2["prompt"],
         "completion_tokens_total": tok["completion"] + tok2["completion"]}
    m["scale_300_docs"] = scale_estimate(m)
    return {"findings": uniq, "rejected": rejected, "review_rejected": review_rejected, "errors": errors,
            "dependencies": deps, "judged": judged, "metrics": m}


def scale_estimate(m, docs_target=300):
    """Линейная экстраполяция: пары на окно и доля после фильтра почти постоянны при фиксированном k."""
    if not m["windows"] or not m["docs"]:
        return {}
    w = m["windows"] / m["docs"] * docs_target
    per_win = m["candidate_pairs"] / m["windows"]
    frac = m["passed_filter"] / max(1, m["candidate_pairs"])
    calls = w * per_win * frac
    rv = m.get("review") or {}
    k2 = rv.get("llm_calls", 0) / max(1, m["llm_calls"])            # вызовов перепроверки на вызов этапа 1
    tin = (m["prompt_tokens"] + rv.get("prompt_tokens", 0)) / max(1, m["llm_calls"])
    tout = (m["completion_tokens"] + rv.get("completion_tokens", 0)) / max(1, m["llm_calls"])
    secs = m["seconds_llm"] + m.get("seconds_review", 0)
    rate = m["llm_calls"] / secs if secs else None
    return {"docs": docs_target, "windows": round(w), "candidate_pairs": round(w * per_win),
            "llm_calls_no_cap": round(calls * (1 + k2)), "prompt_tokens": round(calls * tin), "completion_tokens": round(calls * tout),
            "hours_at_measured_rate": round(calls / rate / 3600, 1) if rate else None,
            "note": "без бюджета, этапы 1+2; tf-idf индекс линейный; эмбеддинги — нужен ANN/numpy вместо полной матрицы"}


# ---------------------------------------------------------------- отчёт
def write_outputs(report, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "conflicts.json").write_text(json.dumps(
        {k: report.get(k, []) for k in ("findings", "review_rejected", "rejected", "errors", "dependencies", "judged", "metrics")}, ensure_ascii=False, indent=2))
    (out_dir / "conflicts.md").write_text(render_md(report))


def _loc(r):
    return f"{r['department']} · {r['doc_title']} · п. {r['clause']}"


VOTER_RU = {"check_ab": "ревизор (A→B)", "check_ba": "ревизор (B→A)", "check_ctx": "ревизор с соседними пунктами"}
KIND_SHORT = {"contradiction": "противоречие", "possible": "возможное", "none": "нет", "dependency": "зависимость",
              "dept_difference": "различие дирекций"}


def render_md(report):
    m = report["metrics"]
    L = ["# Противоречия и несостыковки в регламентах", "",
         f"Документов: {m['docs']}, пунктов-окон: {m['windows']}. Пар-кандидатов: {m['candidate_pairs']}, "
         f"после фильтра: {m['passed_filter']}, проверено LLM: {m['llm_calls']}. "
         f"Найдено: " + ", ".join(f"{KIND_RU.get(k, k)} — {v}" for k, v in m["findings_by_kind"].items()) +
         f". Отброшено проверкой цитат/чисел: {m['rejected']}"
         + (f", перепроверкой: {m['review']['rejected']} из {m['review']['checked']}" if m.get("review", {}).get("checked") else "")
         + ".", "",
         "Находки опираются только на дословные цитаты пунктов (проверено кодом). «Возможное противоречие» и "
         "«Противоречие» — на решение службы качества; автоматически правило не выбирается.", ""]
    for kind in ("contradiction", "possible", "dependency", "dept_difference"):
        items = [f for f in report["findings"] if f["kind"] == kind]
        if not items:
            continue
        L += [f"## {KIND_RU[kind]} ({len(items)})", ""]
        groups = defaultdict(list)
        for f in items:
            groups[(f["a"]["doc_title"], f["b"]["doc_title"])].append(f)
        for (da, db), fs in groups.items():
            L += [f"### {da}" + ("" if da == db else f" ↔ {db}"), ""]
            for f in fs:
                L += [f"**{f['id']}. п. {f['a']['clause']} ↔ п. {f['b']['clause']}** — {f['situation']}", "",
                      f"- {_loc(f['a'])}: «{f['a']['quote']}»", f"- {_loc(f['b'])}: «{f['b']['quote']}»",
                      f"- Почему: {f['explanation']}"]
                if f["signals"]:
                    L.append("- Сигналы кода: " + "; ".join(SIGNAL_RU[s] for s in f["signals"]) + f" (скор {f['score']})")
                if f.get("reclassified"):
                    L.append(f"- Вид уточнён кодом: {f['reclassified']} (модель: {f['llm_kind']})")
                if f.get("votes"):
                    L.append(f"- Перепроверка (3 независимых судьи): уверенность {f['confidence']:.0%}; "
                             + "; ".join(f"{VOTER_RU.get(v['voter'], v['voter'])} — {KIND_SHORT[v['kind']]}" for v in f["votes"]))
                if kind in ("contradiction", "possible"):
                    L.append("- Требует решения службы качества.")
                L.append("")
    rr = report.get("review_rejected") or []
    if rr:
        L += [f"## Отклонено перепроверкой ({len(rr)}) — на разбор службе качества, по приоритету", "",
              "Этап 1 (мягкий судья) счёл несостыковкой, большинство из трёх перепроверок (чек-лист, решает код) — нет. "
              "Перепроверка локальной моделью склонна «примирять» пункты, поэтому раздел упорядочен по приоритету "
              "(сигналы кода + согласие двух проходов этапа 1): начинайте разбор сверху.", ""]
        for f in rr:
            L += [f"**{f['id']}. {_loc(f['a'])} ↔ п. {f['b']['clause']}** — этап 1: {KIND_SHORT.get(f.get('stage1_kind', f['kind']), f['kind'])}"
                  f" (проходы: {', '.join(KIND_SHORT.get(k, k) for k in f.get('stage1_votes', []))}; приоритет {f.get('priority')})",
                  f"- «{f['a']['quote']}» ↔ «{f['b']['quote']}»", f"- Этап 1: {f['explanation']}"]
            for v in f.get("votes", []):
                L.append(f"- {VOTER_RU.get(v['voter'], v['voter'])} — {KIND_SHORT[v['kind']]}: {v['reason']}")
            L.append("")
    deps = report["dependencies"]
    if deps:
        L += [f"## Зависимости от других документов и пунктов ({len(deps)}, найдены кодом)", ""]
        for d in deps:
            L.append(f"- {_loc(d['a'])} → {d['target']}: «{d['a']['quote']}»")
        L.append("")
    L += ["## Метрики", "", "```json", json.dumps(m, ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(L)
