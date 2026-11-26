"""Один ход диалога: тип вопроса → поиск → уточнение или ответ → письмо в СК при отсутствии опоры.

Исходы (инвариант «ни с чем не уходит», SPEC п. 88):
  answer / answer_with_caveat / conflict — ответ + источники;
  clarify — вопрос с вариантами (подразделения из выдачи + «несколько» + «не знаю»);
  escalate — опоры нет: письмо в службу качества, сотрудник в копии;
  out_of_scope / task_refusal — не про регламенты: отказ + куда обратиться.
"""
import re
import threading
from dataclasses import dataclass, field

from concurrent.futures import ThreadPoolExecutor

from app.answer.cache import norm_q
from app.answer.core import SYSTEM as ANSWER_SYSTEM, answer as llm_answer, build_user
from app.answer.understand import understand
from app.chunking.clauses import ref_label
from app.config import aliases as dept_aliases, dept_ids, domain, titles as dept_titles
from app.dialog.clarify import label, mentioned

TASK_VERBS = re.compile(r"^\s*(?:пожалуйста,?\s*)?(сделай|напиши|составь|подготовь|проанализируй|переведи|сгенерируй|придумай|"
                        r"оформи|сформулируй|посчитай|нарисуй|разбери|собери|открой|запроси|выпиши|отправь|создай|заполни|"
                        r"сократи|перескажи мне|набросай|проверь мой|исправь)\b|\bза меня\b", re.I)
# (7) шире: «в каких из доступных документов», «где расписана/написано/указано»
FIND_DOC = re.compile(r"\bв\s+как\w*(\s+[\w-]+){0,3}?\s+(документ|регламент)\w*|"
                      r"\bкак(ой|ие|ом)\s+(документ|регламент)\w*\s+(описыва|содерж|объясня|регулиру|расписыва|есть)\w*|"
                      r"\bгде(\s+[\w-]+){0,2}?\s+(описан|прописан|расписан|написан|указан|изложен|найти|лежит|почитать|посмотреть)\w*|"
                      r"\bкакие\s+(два\s+)?документ\w*(\s+[\w-]+)?\s+(описыва|содерж|отдельно)\w*", re.I)
OUT_OF_SCOPE = re.compile(r"\b(как\w*\s+марж\w*\s+постав|марж\w*\s+(по|моей|на)\s|преми\w*|бонус\w*|kpi|курс\s+(доллар|евро|валют)\w*|"
                          r"конкурент\w*|зарплат\w*|отпуск\w*|больничн\w*|постав\w*\s+марж\w*\s+\d|марж\w*\s+\d+\s*%\s+или)", re.I)
PER_DOC = 16      # фрагментов на документ после слияния запросов
GLOBAL_K = 16     # общий поиск по всей базе: фрагментов на формулировку
PER_DOC_GLOBAL = 4 # в общем поиске — не больше окон одного документа
MAX_DEPTS = 3     # дирекций, по которым готовим ответ, — не больше (на большой базе)
DOCS_PER_DEPT = 3 # документов дирекции для подробного поиска — не больше
CTX_CHARS = 21000 # бюджет контекста на вызов в знаках (решение 24.11, ≈ 6,4 тыс. токенов)
MAX_CTX_TOKENS, MAX_OUT_TOKENS = 16384, 1500   # vLLM max-model-len и лимит ответа
PER_DEPT = 18     # фрагментов на дирекцию в контексте модели (документы маленькие, контекст позволяет)
CLARIFY_FLOOR = 0.15      # документ считается «с опорой», если его лучший скор ≥ max(FLOOR, RATIO × лучший общий)
CLARIFY_RATIO = 0.3
DOC_WORDS = re.compile(r"документ|регламент|раздел|пункт|правил|описан|прописан|расписан|написан|указан|изложен|где\s+(у\s+нас\s+)?(про|найти|посмотреть|почитать)\b", re.I)  # q042: «откуда скачать эксель» — не «где описано»
DOC_RATIO = 0.25           # «где описано»: документ называем, если его пункт не слабее четверти лучшего (q208 — 0,08; q206 — второй документ ИБ)
OOS_SUPPORT = 0.5          # «вне регламентов» по разбору вопроса — только если в регламентах нет сильной опоры
NO_SUPPORT = 0.05          # ниже — опоры нет (v1: 0.30, v2: 0.12 — терялись верные ответы; дальше решает verdict модели)


def scope_system():
    return ("Ты классифицируешь вопрос сотрудника. База знаний — " + domain() + ". Ответь JSON: "
            "in_scope=true, если вопрос о том, как устроены эти процессы в компании (даже если ответа может не быть); "
            "вопросы о правилах, сроках, порогах, санкциях, контактах, ролях и процедурах компании, о проведении проектных "
            "мероприятий (ретроспектива, приёмка, доступы, статус сотрудников в проекте) — in_scope, даже если ответа в "
            "регламентах может не быть. false — только если вопрос о другом: экономика конкретной сделки сотрудника "
            "(какую маржу поставить, какая будет премия), текущие данные конкретной сделки или проекта (сколько денег осталось "
            "сейчас, какой статус у моей заявки), рынок и конкуренты, законы вообще, бытовое.")


SCOPE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["in_scope"],
                "properties": {"in_scope": {"type": "boolean"}}}


@dataclass
class Turn:
    outcome: str
    text: str = ""
    options: list = field(default_factory=list)
    answers: list = field(default_factory=list)      # по подразделению: {doc_id, label, result}
    route: dict = None
    trace: list = field(default_factory=list)
    hits: list = field(default_factory=list)
    documents: list = field(default_factory=list)     # для «где регламент»: [{title, label, sections}]
    external_refs: list = field(default_factory=list)
    tokens: dict = field(default_factory=dict)        # для расчёта экономики: вызовы и токены модели за ход
    question: str = ""                                # вопрос, по которому дан ход (после уточнения темы — уточнённый)


COMPLEX_NOTE = ("\n\n(Это комплексный проект нескольких дирекций. Ответь для части работ, которую ведёт {dept}, по её регламенту. "
                "То, что в регламенте нет правил о совместных проектах или о других дирекциях, — не причина для no_answer.)")


class Counted:
    """Считает вызовы и токены модели за ход (расчёт экономики — по ценам провайдера, решение организаторов 26.11)."""

    STEPS = (("готовишь поиск", "Разбор вопроса"), ("классифицируешь", "Проверка темы"), ("Сравни ответы", "Сравнение дирекций"),
             ("помощник сотрудников компании по внутренним регламентам", "Ответ по дирекции"))

    def __init__(self, llm):
        self.llm, self.t = llm, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "by_step": {}}

    def __getattr__(self, name):          # count_tokens и прочее — к настоящему клиенту
        return getattr(self.llm, name)

    def chat_json(self, *a, **kw):
        res, meta = self.llm.chat_json(*a, **kw)
        pt, ct = (meta or {}).get("prompt_tokens") or 0, (meta or {}).get("completion_tokens") or 0
        self.t["calls"] += 1
        self.t["prompt_tokens"] += pt
        self.t["completion_tokens"] += ct
        system = a[0] if a else kw.get("system", "")
        step = next((name for key, name in self.STEPS if key in system), "Другое")
        b = self.t["by_step"].setdefault(step, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        b["calls"] += 1
        b["prompt_tokens"] += pt
        b["completion_tokens"] += ct
        return res, meta


ASK_DONT_KNOW = "Не знаю"
ASK_SEND = "Отправить вопрос в службу качества"
NOT_COVERED = "Ваш случай в регламенте прямо не оговорён — я отправил вопрос в службу качества, вы в копии. Ниже — правило, которое есть."

CONFLICT = ("Пункты регламента здесь противоречат друг другу — однозначного ответа нет. Я отправил вопрос в службу качества, "
            "вы в копии. Ниже — оба пункта.")


def text_hash(text):
    """Отпечаток текста пункта: пара противоречия подтверждена для ЭТОГО текста; перенумерация или новая редакция —
    отпечаток не совпал → пара не срабатывает, пока служба качества не подтвердит заново (ревью Fable)."""
    import hashlib
    return hashlib.sha1(re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()[:600].encode()).hexdigest()[:12]


def load_conflicts(windows, path=None, log=None):
    """Реестр подтверждённых противоречий (config/conflicts_confirmed.json) → {(doc_id, пункт): [пара…]}.
    Ключ — документ, а не подразделение (у ИБ два документа с совпадающими номерами пунктов)."""
    import json
    from pathlib import Path
    path = Path(path) if path else Path(__file__).resolve().parents[1] / "config" / "conflicts_confirmed.json"
    if not path.exists():
        return {}
    by_doc = {}
    for w in windows:
        by_doc.setdefault((w.get("doc_id"), w.get("department")), {}).setdefault(w.get("clause"), w)
    out = {}
    for p in json.loads(path.read_text()).get("pairs", []):
        for (doc_id, dep), cl in by_doc.items():
            if dep != p["department"] or p["a"] not in cl or p["b"] not in cl:
                continue
            if p.get("doc") and p["doc"].lower() not in (cl[p["a"]].get("path", "").lower()):
                continue
            wa, wb = cl[p["a"]], cl[p["b"]]
            if (p.get("hash_a") and p["hash_a"] != text_hash(wa["text"])) or (p.get("hash_b") and p["hash_b"] != text_hash(wb["text"])):
                if log is not None:
                    log.append(f"пара {p['a']}↔{p['b']} ({dep}) не применена: текст пункта изменился — подтвердить заново")
                continue
            pid = f"{dep}:{p['a']}↔{p['b']}"
            for x, wy in ((wa, wb), (wb, wa)):
                out.setdefault((doc_id, x["clause"]), []).append(
                    {"clause": wy["clause"], "why": p.get("why", ""), "text": wy["text"], "pair": pid})
    return out


def _on_topic(question, c):
    """Пара срабатывает, только если второй пункт пары по теме вопроса (ревью Fable: q081, q101, q134 — не по теме)."""
    from app.answer.core import stems
    return len(stems(question) & stems(c["text"] + " " + c["why"])) >= 2


def apply_conflicts(results, conflicts, tr, question=""):
    """Ответ опирается на пункт из подтверждённой пары и второй пункт пары по теме вопроса → оба пункта, «противоречие»."""
    if not conflicts:
        return results
    out = []
    for r in results:
        res = r["result"]
        if res["verdict"] == "no_answer":
            out.append(r)
            continue
        hits = []
        for st in res.get("steps", []):
            for c in conflicts.get((st.get("doc_id"), st.get("clause")), []):
                if c not in hits and (not question or _on_topic(question, c)):
                    hits.append(c)
        if hits:
            note = "; ".join(f"п. {c['clause']}: {c['why']}" for c in hits)
            res = {**res, "verdict": "conflict",
                   "caveat": ((res.get("caveat") or "") + " Противоречие в регламенте — " + note).strip(),
                   "conflict_with": hits}
            tr.append(f"{r['label']}: пункт ответа входит в подтверждённое противоречие — показываю оба пункта")
        out.append({**r, "result": res})
    return out


SAME_SYSTEM = ("Сравни ответы разных дирекций на один вопрос сотрудника. Сравнивай ТОЛЬКО ответ на сам вопрос: если спрашивают "
               "«кто» — совпадает ли ответственная роль; «когда/за сколько» — совпадает ли срок; «можно ли» — совпадает ли решение и "
               "условие. same=true, если по сути вопроса ответы одинаковы, даже если одна дирекция приводит больше подробностей, "
               "шагов или систем (лишние детали — не различие). same=false, если отличается сама роль, срок, число, решение или "
               "условие, отвечающее на вопрос. difference — коротко, в чём различие по сути вопроса.")
SAME_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["same", "difference"],
               "properties": {"same": {"type": "boolean"}, "difference": {"type": "string", "maxLength": 200}}}


def answers_same(llm, question, answered):
    """Разбор v4 п. 1b, судья v7: в 10 вопросах «одинаково во всех дирекциях» ассистент зря спрашивал дирекцию."""
    blocks = []
    for r in answered:
        res = r["result"]
        steps = "; ".join(f"{st.get('text', '')} ({st.get('who', '')} {st.get('deadline', '')})".strip() for st in res.get("steps", []))
        blocks.append(f"{r['label']}: {res.get('summary', '')} Шаги: {steps} Оговорка: {res.get('caveat', '')}")
    try:
        js, _ = llm.chat_json(SAME_SYSTEM, f"Вопрос: {question}\n\n" + "\n\n".join(blocks), SAME_SCHEMA, max_tokens=120)
        return bool(js.get("same")), js.get("difference", "")
    except Exception:
        return False, ""


DEEP_SUPPORT = 0.5         # «нет ответа» при опоре не слабее — повод копнуть глубже (судья: q085, q094, q122)
REF_RE = re.compile(r"(?:п\.|пункт\w*|пп\.)\s*(\d+(?:\.\d+)+)", re.I)
VERDICT_RANK = {"answer": 3, "conflict": 2, "answer_with_caveat": 2, "no_answer": 0}


def build_index(windows):
    """(документ, пункт) → окно — для агентного прохода: пункт по ссылке «п. X.Y» и соседние пункты.
    Ключ — документ, а не подразделение: у ИБ два документа (пресейл и проект) с 75 совпадающими номерами пунктов
    (ревью «перенос из сборки» Н1) — соседний пункт брался бы из чужого документа."""
    idx = {}
    for w in windows:
        idx.setdefault((w.get("doc_id"), w.get("clause")), w)
    return idx


def _neighbors(clause):
    parts = clause.split(".")
    out = []
    try:
        last = int(parts[-1])
    except ValueError:
        return out
    for d in (-1, 1):
        if last + d > 0:
            out.append(".".join(parts[:-1] + [str(last + d)]))
    if len(parts) > 2:
        out.append(".".join(parts[:-1]))
    return out


def needs_deep(res, support):
    if res["verdict"] == "answer_with_caveat" and (res.get("missing") or "").strip():
        return "в ответе не хватает: " + res["missing"].strip()[:120]
    if res["verdict"] == "no_answer" and support >= DEEP_SUPPORT:
        return "пункты по теме нашлись, а ответа нет"
    return ""


def deep_pass(llm, question, dep, label_, ws, res, meta, search_fn, docs, index, answer_cache, why):
    """Агентный проход (идея владельца 26.11: слабые ответы — сразу через агента): добрать пункты, на которые ссылается
    текст, соседние пункты и пункты по недостающему, ответить второй раз и оставить лучший ответ."""
    have = {w["window_id"] for w in ws}
    extra = []
    doc_of = {w["window_id"]: w.get("doc_id") for w in ws}
    cited = [(doc_of.get(st.get("window_id")), st.get("clause")) for st in res.get("steps", [])] or [(w.get("doc_id"), w["clause"]) for w in ws[:3]]
    for w in ws[:8]:
        for c in REF_RE.findall(w.get("text", "")):
            x = index.get((w.get("doc_id"), c)) if index else None
            if x and x["window_id"] not in have:
                extra.append({**x, "score": w.get("score", 0) * 0.9, "via": f"ссылка из п. {w['clause']}"})
    for d, c in cited:
        for n in _neighbors(c or ""):
            x = index.get((d, n)) if index else None
            if x and x["window_id"] not in have:
                extra.append({**x, "score": 0.5, "via": f"соседний к п. {c}"})
    miss = (res.get("missing") or "").strip()
    if miss and docs:
        for h in _search(search_fn, miss, set(docs), 6):
            if h["window_id"] not in have:
                extra.append({**h, "via": "по недостающему"})
    uniq = {}
    for w in extra:
        uniq.setdefault(w["window_id"], w)
    extra = list(uniq.values())
    if not extra:
        return None
    base = {"department_name": ws[0]["department_name"], "doc_title": ws[0]["doc_title"]} if ws else {}
    extra = [{**base, **w, "doc_title": w.get("doc_title") or base.get("doc_title", "")} for w in extra]
    ws2 = budget(sorted(ws + extra, key=lambda w: -w.get("score", 0)), max_windows=PER_DEPT + 6)
    ws2 = fit_tokens(llm, question, ws2, label_)
    res2, meta2 = cached_answer(llm, question, ws2, label_, answer_cache)
    better = (VERDICT_RANK.get(res2["verdict"], 0), len(res2.get("steps", []))) > (VERDICT_RANK.get(res["verdict"], 0), len(res.get("steps", [])))
    return {"res": res2, "meta": meta2, "ws": ws2, "better": better, "added": len(extra), "why": why}


def outcome_of(results):
    verdicts = [r["result"]["verdict"] for r in results]
    return "conflict" if "conflict" in verdicts else ("answer_with_caveat" if "answer_with_caveat" in verdicts else "answer")


PRACTICE_COVERS = 0.5      # подтверждённая практика СК с таким скором в контексте дирекции — случай уже разобран экспертом


def practice_covers(r):
    """Кейс 3: после подтверждения эксперта тот же вопрос не должен снова уходить письмом в СК."""
    if any(st.get("doc_title", "").startswith("Практика") for st in r["result"].get("steps", [])):
        return True
    return any(w.get("kind") == "practice" and w.get("score", 0) >= PRACTICE_COVERS for w in r.get("windows") or [])


def not_covered(results, complex_=False):
    """Автописьмо «случай не оговорён» — только по ответу одной дирекции и не в комплексном проекте
    (v6: письмо уходило на каждую оговорку, в 10 из 15 комплексных проектов)."""
    shown = [r for r in results if r["result"]["verdict"] != "no_answer"]
    if complex_ or len(shown) != 1:
        return False
    r = shown[0]
    return r["result"]["verdict"] == "answer_with_caveat" and r["result"].get("case_covered") is False and not practice_covers(r)


def grounded_options(options, windows, steps=()):
    """Варианты уточнения — словами процитированных пунктов (ревью Fable К7: по всем 18 окнам проходило что угодно,
    а «Да/Нет» не проходили никогда). «Да»/«Нет»/«Есть» — допустимы; «не определено», «экспертиза» — нет."""
    from app.answer.core import NUM, stems
    cited = [st.get("source_text", "") for st in steps if st.get("source_text")] or [w.get("text", "") for w in windows]
    text = " ".join(cited)
    st_text, nums = stems(text), set(NUM.findall(text))
    out = []
    for o in options:
        o = o.strip()
        lo = o.lower()
        if not o or re.search(r"не\s*определ|экспертиз|неизвест|затрудняюсь", lo):
            continue
        if set(NUM.findall(o)) - nums:
            continue
        if re.match(r"^(да|нет|есть|ещё нет|еще нет)\b", lo) or stems(o) & st_text:
            out.append(o)
    return out[:4]


def in_scope_llm(llm, question):
    try:
        sc, _ = llm.chat_json(scope_system(), question, SCOPE_SCHEMA, max_tokens=20)
        return bool(sc.get("in_scope", True))
    except Exception:
        return True


REF_TOP, REF_MIN = 3, 0.5   # отсылка к внешнему документу при «нет ответа» — из 3 лучших пунктов со скором ≥ 0,5


_BASE_TITLES = threading.local()      # названия документов в базе — для сверки отсылок (заполняет run_turn)


def _in_base(ref):
    """Отсылка указывает на документ, который ЕСТЬ в базе (ревью Fable: на 300 регламентах «нет в базе» было бы ложным)."""
    from app.answer.core import stems
    st = stems(ref)
    if not st:
        return False
    for title in getattr(_BASE_TITLES, "titles", ()):
        tt = stems(title)
        if tt and len(st & tt) / len(st) >= 0.6:
            return True
    return False


def external_only(refs):
    return sorted(r for r in refs if not _in_base(r))


def top_refs(hits):
    return external_only({ref for h in hits[:REF_TOP] if h.get("score", 0) >= REF_MIN for ref in h.get("external_refs", [])})


def cited_refs(results):
    """(6) Внешние документы — только из пунктов, на которые опирается ответ, и только те, которых нет в базе."""
    return external_only({ref for r in results for st in r["result"].get("steps", []) for ref in st.get("external_refs", [])})


def _search(search_fn, q, allowed, k):
    try:
        return search_fn(q, allowed, k)
    except TypeError:                 # старая сигнатура search_fn(q, allowed)
        return search_fn(q, allowed)


def budget(ws, chars=CTX_CHARS, max_windows=PER_DEPT):
    """Окна по убыванию скора, пока не исчерпан бюджет в знаках; первое окно берётся всегда."""
    out, used = [], 0
    for w in ws:
        n = len(w.get("text", ""))
        if out and (used + n > chars or len(out) >= max_windows):
            continue
        out.append(w)
        used += n
    return out


def fit_tokens(llm, q, ws, label_):
    """Проверка длины перед вызовом (SPEC §6): вход + ответ ≤ max-model-len; не влезает — убираем слабые окна."""
    count = getattr(llm, "count_tokens", None)
    if not count:
        return ws
    for _ in range(3):
        n = count(ANSWER_SYSTEM, build_user(q, ws, label_))
        if n is None or n + MAX_OUT_TOKENS <= MAX_CTX_TOKENS or len(ws) <= 1:
            return ws
        keep = max(1, int(len(ws) * (MAX_CTX_TOKENS - MAX_OUT_TOKENS) / n) - 1)
        ws = ws[:keep]
    return ws


def cached_answer(llm, q, ws, label_, cache):
    if cache is None:
        return llm_answer(llm, q, ws, label_)
    k = cache.key("answer", norm_q(q), label_, tuple(w["window_id"] for w in ws))
    hit = cache.get(k)
    if hit is not None:
        return hit[0], {**hit[1], "from_cache": True}
    res, meta = llm_answer(llm, q, ws, label_)
    cache.put(k, (res, meta))
    return res, meta


def plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def strip_departments(question, registry=()):
    """Названия подразделений мешают поиску внутри документа одного подразделения."""
    q = question
    for pats in dept_aliases(list(registry)).values():
        for pat in pats:
            q = re.sub(pat, " ", q, flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"\s*\+\s*", " ", q)).strip(" :;,.—-") or question


def route_for(question):
    """Куда обратиться с вопросом вне регламентов — правила из config/documents.json «routes» (судья: 9 расхождений с эталоном)."""
    from app.config import cfg
    q = question.lower()
    for r in cfg().get("routes") or []:
        if re.search(r["match"], q, re.I):
            return {"to": r["to"], "why": r.get("why", "")}
    return {"to": "ваш руководитель", "why": "вопрос не относится к регламентам проектной деятельности"}


def _docs_for(depts, stage, registry):
    out = []
    for d in registry:
        if d["department"] not in depts:
            continue
        same = [x for x in registry if x["department"] == d["department"]]
        if len(same) > 1 and stage in ("presale", "project") and d["stage"] != stage:
            continue
        out.append(d["doc_id"])
    return out


def run_turn(question, registry, search_fn, llm, session=None, workers=4, answer_cache=None, conflicts=None, index=None):
    """search_fn(query, allowed) -> hits (score реранкера).
    session (мутируется): docs, clarifications, no_clarify, complex; cache — ответы по дирекциям до выбора;
    ask — открытый уточняющий вопрос (условие или тема); ask_reply — ответ сотрудника на него."""
    session = {} if session is None else session
    c = Counted(llm)
    session["_conflicts"], session["_index"] = conflicts, index
    _BASE_TITLES.titles = [d.get("display_title") or d.get("title") or "" for d in registry] + \
                          [d.get("title") or "" for d in registry]
    t = _run_turn(question, registry, search_fn, c, session, workers, answer_cache)
    t.question = t.question or session.get("_effective_q") or question
    session.pop("_effective_q", None)
    session.pop("_conflicts", None)
    session.pop("_index", None)
    t.tokens = c.t
    return t


def _ask_turn(session, kind, question, text, options, tr, hits, base=None, dep=None, ws=None):
    session["ask"] = {"kind": kind, "q": question, "text": text, "options": options, "base": base, "dep": dep, "ws": ws}
    return Turn("ask", text, options=[{"doc_id": f"a{i}", "label": o} for i, o in enumerate(options)], trace=tr, hits=hits)


def _final(results, note, tr, hits, session, question, complex_=False):
    """Ответ готов. Одна дирекция и ответ зависит от условия, не сказанного в вопросе, → уточняющий вопрос (кейс 2);
    правило есть, а случай не оговорён → ответ с оговоркой + автоматическое письмо в службу качества (кейс 1)."""
    results = apply_conflicts(results, session.get("_conflicts"), tr, question)
    shown = [r for r in results if r["result"]["verdict"] != "no_answer"]
    if len(shown) == 1 and not session.get("no_clarify") and not any(r["result"]["verdict"] == "conflict" for r in shown) and session.get("clarifications", 0) < 2:
        r = shown[0]
        opts = grounded_options(r["result"].get("ask_options") or [], r.get("windows") or [], r["result"].get("steps") or [])
        if r["result"].get("ask", "").strip() and len(opts) >= 2:
            session["clarifications"] = session.get("clarifications", 0) + 1
            tr.append(f"{r['label']}: ответ зависит от условия, которого нет в вопросе, — уточняю")
            t = _ask_turn(session, "condition", question, r["result"]["ask"].strip(), opts[:4] + [ASK_DONT_KNOW], tr, hits,
                          base=r, dep=r["doc_id"], ws=r.get("windows"))
            session["ask"]["hits"] = hits[:5]
            return t
    t = Turn(outcome_of(results), note, answers=results, trace=tr, hits=hits, external_refs=cited_refs(results))
    if t.outcome == "answer" and t.external_refs:
        # ответ опирается на пункт, который отсылает к документу вне базы, — ответ неполный: оговорка (v7 q154, q155)
        t.outcome = "answer_with_caveat"
        tr.append("Пункт ответа отсылает к документу, которого нет в базе, — ответ неполный")
    if t.outcome == "conflict" and not any(r["result"].get("conflict_with") for r in results) and not any(
            len({st.get("clause") for st in r["result"].get("steps", [])}) >= 2 for r in results if r["result"]["verdict"] == "conflict"):
        t.outcome = "answer_with_caveat"            # «противоречие» со слов модели без двух проверенных пунктов — оговорка, не письмо
        tr.append("Модель отметила противоречие, но не опёрла его на два пункта — показываю как ответ с оговоркой")
    if t.outcome == "conflict":
        tr.append("Пункты регламента противоречат друг другу — отправляю вопрос в службу качества")
        t.text = CONFLICT + (" " + note if note else "")
        pairs = sorted({c["pair"] for r in results for c in r["result"].get("conflict_with", []) if c.get("pair")})
        t.route = {"to": "служба качества", "email": True,
                   "why": "противоречие между пунктами регламента" + (": " + ", ".join(pairs) if pairs else "")}
    elif not_covered(results, complex_):
        tr.append("Правило есть, но случай сотрудника в нём не оговорён — отправляю вопрос в службу качества")
        t.text = (NOT_COVERED + (" " + note if note else ""))
        t.route = {"to": "служба качества", "email": True, "why": "случай не оговорён"}
    return t


def _run_turn(question, registry, search_fn, llm, session, workers, answer_cache=None):
    tr = []

    by_id = {x["doc_id"]: x for x in registry}
    ask, reply = session.get("ask"), session.pop("ask_reply", None)
    if ask and reply is not None and ask["q"] == question:
        session.pop("ask", None)
        if ask["kind"] == "topic":
            if reply == ASK_SEND:
                tr.append("Сотрудник попросил отправить вопрос в службу качества")
                return Turn("escalate", "Я отправил вопрос в службу качества, вы в копии — ответ придёт на почту.",
                            route={"to": "служба качества", "email": True}, trace=tr)
            tr.append(f"Сотрудник уточнил тему: {reply}")
            session["_effective_q"] = f"{question} ({reply})"      # дальше по диалогу — уточнённый вопрос (Fable К2)
            return _run_turn(f"{question} ({reply})", registry, search_fn, llm, session, workers, answer_cache)
        base = ask["base"]
        if reply == ASK_DONT_KNOW:
            tr.append("Условие неизвестно — показываю правило со всеми условиями; однозначный ответ дать нельзя")
            return Turn("answer_with_caveat", "Без этого условия однозначно ответить нельзя. Ниже — правило и от чего оно зависит; "
                        "если условие неясно, задайте вопрос службе качества.", answers=[base], trace=tr,
                        hits=ask.get("hits") or [], external_refs=cited_refs([base]))
        tr.append(f"Сотрудник уточнил условие: {reply}")
        q2 = f"{question}\n\nУточнение сотрудника: {ask['text']} — {reply}"
        res, meta = llm_answer(llm, q2, ask["ws"], base["label"])
        r = {**base, "result": res, "meta": meta}
        if res["verdict"] == "no_answer":
            r = base
        tr.append(f"{r['label']}: ответ для варианта «{reply}»")
        out = Turn(outcome_of([r]), answers=[r], trace=tr, external_refs=cited_refs([r]))
        if not_covered([r]):
            out.text, out.route = NOT_COVERED, {"to": "служба качества", "email": True, "why": "случай не оговорён"}
        return out
    cache = session.get("cache")
    if cache and cache["q"] == question and session.get("docs"):
        chosen = {by_id[d]["department"] for d in session["docs"] if d in by_id}
        cached = [cache["answers"][d] for d in cache["order"] if d in chosen]
        if cached and chosen <= set(cache["answers"]):
            tr.append("Ответы по дирекциям уже готовы — показываю " + ("выбранную" if len(cached) == 1 else "выбранные"))
            hits = cache["hits"]
            return _final(cached, "", tr, hits, session, question, bool(session.get("complex")) or len(cached) > 1)

    # 1. понять вопрос (агентный шаг)
    # скорость (ревью Fable): общий поиск по исходной формулировке идёт параллельно с разбором вопроса моделью;
    # результаты поисков за ход кэшируются — повторный поиск той же формулировки в той же области не делается
    _scache, _slock = {}, threading.Lock()

    def search_c(q, allowed, k):
        key = (q, frozenset(allowed) if allowed else None, k)
        with _slock:
            if key in _scache:
                return _scache[key]
        res = _search(search_fn, q, allowed, k)
        with _slock:
            _scache[key] = res
        return res
    pre_named = mentioned(question, registry)
    pre_scope = None if not (session.get("docs") or pre_named) else set(pre_named or session.get("docs"))
    pre = threading.Thread(target=lambda: search_c(strip_departments(question, registry), pre_scope, GLOBAL_K), daemon=True)
    pre.start()
    uk = answer_cache.key("understand", norm_q(question)) if answer_cache else None
    u = answer_cache.get(uk) if uk else None
    if u is None:
        u, _ = understand(llm, question, registry)
        if uk and "error" not in u:
            answer_cache.put(uk, u)
    # тип вопроса определяет модель (универсально); регулярные выражения — запасной путь, если разбор не удался
    intent = u.get("intent") or ""
    if intent == "task" or (not intent and TASK_VERBS.search(question)):
        tr.append("Это просьба выполнить работу, а не вопрос по регламентам — отказ")
        return Turn("task_refusal", "Я отвечаю только на вопросы по регламентам компании. Для таких задач в компании есть другие инструменты.",
                    route=route_for(question), trace=tr)
    if not intent and OUT_OF_SCOPE.search(question):
        tr.append("Вопрос не про регламенты — подсказываю, куда обратиться")
        r = route_for(question)
        return Turn("out_of_scope", f"Этот вопрос не касается базы регламентов. Обратитесь: {r['to']}.", route=r, trace=tr)
    # исходная формулировка всегда в поиске: переписывание может увести в сторону (26.11: «заявка» → «РТЗ»)
    queries = list(dict.fromkeys([strip_departments(question, registry)] + [strip_departments(x, registry) for x in u["queries"]]))[:4]
    tr.append("Понял вопрос как: " + " · ".join(queries[1:] or queries))
    named = mentioned(question, registry) or _docs_for(set(u.get("departments") or []), u.get("stage", ""), registry)
    if named:
        tr.append("Дирекция названа в вопросе")
    # названная в вопросе дирекция важнее выбранной ранее в диалоге (ревью Fable 26.11: «А в ДБРЦТ…» после выбора ТД
    # получал ответ ТД); выбранная ранее — для продолжений без названия дирекции (SPEC п. 5)
    if named and session.get("docs") and set(named) != set(session["docs"]):
        session["docs"] = []
        tr.append("В вопросе названа другая дирекция — отвечаю по ней")
    scope = list(named or session.get("docs") or [d["doc_id"] for d in registry])

    # 2. поиск в два шага (универсальная версия): сначала по всей доступной базе — один раз на формулировку,
    #    затем вглубь — только в лучших документах лучших дирекций. Число поисков не растёт с размером базы:
    #    формулировки × (1 + дирекции ≤ MAX_DEPTS × документы ≤ DOCS_PER_DEPT).
    def merged(allowed, k):
        """Слияние выдач по формулировкам: RRF по рангам — пункт, лучший для одной из формулировок,
        не вытесняется пунктами с высоким скором по другим (26.11: ДБРЦТ п. 1.5)."""
        best, rrf = {}, {}
        for qq in queries:
            for r, h in enumerate(search_c(qq, allowed, k)):
                wid = h["window_id"]
                rrf[wid] = rrf.get(wid, 0) + 1 / (10 + r)
                if wid not in best or h["score"] > best[wid]["score"]:
                    best[wid] = h
        order = sorted(best, key=lambda w: -rrf[w])
        if allowed is None:
            # общий шаг: не больше PER_DOC_GLOBAL окон одного документа — иначе шаблонные регламенты (копии, редакции)
            # забивают выдачу и другие дирекции не попадают в ответ (ревью Fable К10, scale-test)
            per, kept = {}, []
            for w in order:
                d = best[w]["doc_id"]
                if per.get(d, 0) < PER_DOC_GLOBAL:
                    per[d] = per.get(d, 0) + 1
                    kept.append(w)
            order = kept
        order = order[:k]
        return sorted((best[w] for w in order), key=lambda h: -h["score"])

    pre.join(timeout=15)                     # предварительный общий поиск уже готов — merged() возьмёт его из кэша хода
    whole = not session.get("docs") and not named
    global_hits = merged(None if whole else set(scope), GLOBAL_K)
    doc_best = {}
    for h in global_hits:
        if h["doc_id"] in by_id:
            doc_best[h["doc_id"]] = max(doc_best.get(h["doc_id"], 0), h["score"])
    scope_depts = list(dict.fromkeys(by_id[d]["department"] for d in scope if d in by_id))
    dept_best = {}
    for d, sc in doc_best.items():
        dept_best[by_id[d]["department"]] = max(dept_best.get(by_id[d]["department"], 0), sc)
    if len(scope_depts) <= MAX_DEPTS:
        cand_depts = scope_depts                       # маленькая база: вглубь во всех (так было на 4 документах)
    else:
        cand_depts = [d for d, _ in sorted(dept_best.items(), key=lambda kv: -kv[1])][:MAX_DEPTS]
    deep = []
    for dep in cand_depts:
        docs = [d for d in scope if d in by_id and by_id[d]["department"] == dep]
        docs.sort(key=lambda d: -doc_best.get(d, 0))
        deep += docs[:DOCS_PER_DEPT]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        per_doc = dict(zip(deep, ex.map(lambda d: merged({d}, PER_DOC), deep)))
    hits = sorted({h["window_id"]: h for h in global_hits + [h for hs in per_doc.values() for h in hs]}.values(),
                  key=lambda h: -h["score"])
    best = hits[0]["score"] if hits else 0
    tr.append((f"Искал по всей базе ({len(scope)} {plural(len(scope), 'регламент', 'регламента', 'регламентов')})" if whole else
               f"Искал в документах дирекции ({len(scope)})") + ", затем подробно — "
              f"в {len(deep)} {plural(len(deep), 'документе', 'документах', 'документах')}; {len(queries)} "
              f"{plural(len(queries), 'формулировка', 'формулировки', 'формулировок')}")

    def escalate_or_scope(results):
        """(4) Перед письмом — проверка «вопрос вообще про процессы компании?»."""
        ext = top_refs(hits)
        if ext:
            tr.append("Пункт отсылает к документу, которого нет в базе, — называю его")
            return Turn("external_ref", "Порядок описан в отдельном документе, которого нет в базе ассистента: " + "; ".join(ext),
                        answers=results, trace=tr, hits=hits, external_refs=ext)
        if not in_scope_llm(llm, question):
            tr.append("Ответа нет, и вопрос не про процессы компании — подсказываю, куда обратиться")
            r = route_for(question)
            return Turn("out_of_scope", f"Этот вопрос не касается базы регламентов. Обратитесь: {r['to']}.", route=r, trace=tr, hits=hits)
        tr.append("В найденных пунктах ответа нет — отправляю вопрос в службу качества")
        return Turn("escalate", "В регламентах прямого ответа нет. Я отправил вопрос в службу качества, вы в копии.",
                    route={"to": "служба качества", "email": True}, answers=results, trace=tr, hits=hits)

    def _finish(results, note=""):
        if all(r["result"]["verdict"] == "no_answer" for r in results):
            return escalate_or_scope(results)
        return _final(results, note, tr, hits, session, question, complex_)

    if intent == "out_of_scope" and best < OOS_SUPPORT:
        tr.append("Вопрос не про процессы компании, и сильной опоры в регламентах нет — подсказываю, куда обратиться")
        r = route_for(question)
        return Turn("out_of_scope", f"Этот вопрос не касается базы регламентов. Обратитесь: {r['to']}.", route=r, trace=tr, hits=hits)

    if best < NO_SUPPORT:
        if not in_scope_llm(llm, question):
            tr.append("Подходящих пунктов нет, и вопрос не про процессы компании — подсказываю, куда обратиться")
            r = route_for(question)
            return Turn("out_of_scope", f"Этот вопрос не касается базы регламентов. Обратитесь: {r['to']}.", route=r, trace=tr, hits=hits)
        tr.append("Подходящих пунктов в регламентах нет — отправляю вопрос в службу качества")
        return Turn("escalate", "В регламентах нет ответа на этот вопрос. Я отправил вопрос в службу качества, вы в копии — ответ придёт на почту.",
                    route={"to": "служба качества", "email": True}, trace=tr, hits=hits)

    # вопрос не сопоставляется с регламентом (размытый) → сначала уточнить тему, а не отвечать наугад
    topics = list(dict.fromkeys(h["section_title"] for h in hits[:8] if h.get("section_title")))[:3]
    if u.get("vague") and len(topics) >= 2 and not session.get("no_clarify") and session.get("clarifications", 0) < 2:
        session["clarifications"] = session.get("clarifications", 0) + 1
        tr.append("Вопрос размытый — предлагаю близкие темы из регламентов")
        return _ask_turn(session, "topic", question, "Уточните, о чём вопрос:", topics + [ASK_SEND], tr, hits)

    if (intent == "find_document" and DOC_WORDS.search(question)) or (not intent and FIND_DOC.search(question)):
        if top_refs(hits):
            ext = top_refs(hits)
            tr.append("Вопрос о том, где описано; найденный пункт отсылает к документу, которого нет в базе")
            return Turn("external_ref", "Порядок описан в отдельном документе, которого нет в базе ассистента: "
                        + "; ".join(ext), trace=tr, hits=hits, external_refs=ext)
        docs_found = []
        for h in [x for x in hits[:6] if x["score"] >= DOC_RATIO * best]:     # q208: не называть документ со слабым пунктом
            if h["doc_id"] not in [d["doc_id"] for d in docs_found] and h["doc_id"] in by_id:
                docs_found.append({"doc_id": h["doc_id"], "title": by_id[h["doc_id"]]["display_title"],
                                   "label": label(by_id[h["doc_id"]], registry), "sections": []})
            dd = next((d for d in docs_found if d["doc_id"] == h["doc_id"]), None)
            sec = ref_label(h) if h.get("kind") == "heading_section" else f"раздел {h['section']} «{h['section_title']}», п. {h['clause']}"
            if dd and sec not in dd["sections"]:
                dd["sections"].append(sec)
        tr.append(f"Вопрос о том, где описано, — нашёл {len(docs_found)} {plural(len(docs_found), 'документ', 'документа', 'документов')}")
        return Turn("document", "Это описано здесь:", documents=docs_found, trace=tr, hits=hits)

    # 3. дирекции, для которых готовим ответ: названные/выбранные или лучшие по опоре (не больше MAX_DEPTS)
    for d, hs in per_doc.items():
        if hs:
            dep = by_id[d]["department"]
            dept_best[dep] = max(dept_best.get(dep, 0), hs[0]["score"])
    names = dept_titles(registry)
    fixed = bool(session.get("docs") or named)
    if fixed:
        depts = sorted(scope_depts, key=lambda x: -dept_best.get(x, 0))
        tr.append("Дирекция " + ("выбрана вами" if session.get("docs") else "названа в вопросе"))
    else:
        depts = [dep for dep, sc in sorted(dept_best.items(), key=lambda kv: -kv[1]) if sc >= NO_SUPPORT][:MAX_DEPTS]
        tr.append(f"Готовлю ответ по каждой дирекции, где нашлись пункты: {len(depts)}")
    # (2) комплексный проект: каждая дирекция отвечает за свою часть
    complex_ = len(depts) >= 2 and (session.get("complex") or len({by_id[d]["department"] for d in named}) >= 2)

    index = session.get("_index")
    # 4. ответ по каждой дирекции — параллельно; контекст — по бюджету в знаках (решение 24.11: ≈ 21 тыс. знаков),
    #    а не по числу окон: на PDF окна до 2 400 знаков, 18 окон переполнили бы контекст модели
    def one(dep):
        ws = []
        for d in [x for x in deep if by_id[x]["department"] == dep]:
            doc = by_id[d]
            for w in per_doc.get(d) or []:
                ws.append({**w, "department_name": names.get(dep, dep),
                           "doc_title": "Практика службы качества" if w.get("kind") == "practice" else doc["display_title"]})
        ws = budget(sorted({w["window_id"]: w for w in ws}.values(), key=lambda w: -w["score"]))
        q = question + (COMPLEX_NOTE.format(dept=names.get(dep, dep)) if complex_ else "")
        ws = fit_tokens(llm, q, ws, names.get(dep, dep))
        try:
            res, meta = cached_answer(llm, q, ws, names.get(dep, dep), answer_cache)
        except Exception as e:             # сбой по одной дирекции не роняет весь ход (ревью Fable К12)
            res = {"verdict": "no_answer", "summary": "", "steps": [], "rejected": [], "caveat": "",
                   "missing": "ответ не получен из-за сбоя модели", "error": str(e)[:120]}
            meta = {"error": str(e)[:120]}
        why = needs_deep(res, dept_best.get(dep, 0)) if not session.get("no_deep") else ""
        if why:
            try:
                d = deep_pass(llm, q, dep, names.get(dep, dep), ws, res, meta, search_fn,
                              [x for x in deep if by_id[x]["department"] == dep], index, answer_cache, why)
            except Exception as e:
                d, meta["deep_error"] = None, str(e)[:120]
            if d:
                meta["deep"] = {"why": why, "added": d["added"], "better": d["better"], "verdict2": d["res"]["verdict"]}
                if d["better"]:
                    res, ws = d["res"], d["ws"]
                    meta.update({k: v for k, v in d["meta"].items() if k in ("prompt_tokens", "completion_tokens")})
        meta["clauses_seen"] = [w["clause"] for w in ws]
        meta["context_chars"] = sum(len(w["text"]) for w in ws)
        return {"doc_id": dep, "label": names.get(dep, dep), "result": res, "meta": meta, "windows": ws}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, depts))
    for r in results:
        res = r["result"]
        verdict = {"answer": "ответ", "answer_with_caveat": "ответ с оговоркой", "conflict": "противоречие",
                   "no_answer": "прямого ответа нет"}.get(res["verdict"], res["verdict"])
        n, rej = len(res["steps"]), len(res["rejected"])
        dp = r["meta"].get("deep")
        if dp:
            tr.append(f"{r['label']}: копнул глубже ({dp['why']}) — добавил {dp['added']} "
                      f"{plural(dp['added'], 'пункт', 'пункта', 'пунктов')}" + (", ответ стал полнее" if dp["better"] else ", первый ответ оставлен"))
        tr.append(f"{r['label']}: {verdict}" + (f", {n} {plural(n, 'шаг', 'шага', 'шагов')} со ссылками на пункты" if n else "")
                  + (f"; {rej} {plural(rej, 'утверждение отброшено', 'утверждения отброшены', 'утверждений отброшено')} проверкой цитат" if rej else "; все цитаты сверены с текстом"))
        r["meta"]["trace_debug"] = f"пункты в контексте: {', '.join(r['meta'].get('clauses_seen', []))}"
    if fixed:
        return _finish(results)

    # 5. решение об уточнении — по verdict модели (разбор v4, исправление 1)
    answered = [r for r in results if r["result"]["verdict"] != "no_answer"]
    silent = [r["label"] for r in results if r["result"]["verdict"] == "no_answer"]   # только проверенные дирекции
    note = ("В регламентах " + (", ".join(silent[:-1]) + " и " + silent[-1] if len(silent) > 1 else silent[0])
            + " об этом не сказано.") if answered and silent else ""
    if len(answered) >= 2 and not session.get("no_clarify") and session.get("clarifications", 0) < 2:
        same, diff = answers_same(llm, question, answered)
        if same:
            tr.append(f"Ответ есть в регламентах {len(answered)} {plural(len(answered), 'дирекции', 'дирекций', 'дирекций')} и по сути одинаков — уточнять не нужно")
            return _finish(answered, ("Порядок одинаков во всех дирекциях, где он описан. " + note).strip())
        if diff:
            tr.append(f"Ответы дирекций различаются: {diff}")
        session["cache"] = {"q": question, "answers": {r["doc_id"]: r for r in answered}, "order": [r["doc_id"] for r in answered],
                            "hits": hits[:5]}
        tr.append(f"Ответ есть в регламентах {len(answered)} {plural(len(answered), 'дирекции', 'дирекций', 'дирекций')} и отличается — уточняю дирекцию")
        opts = [{"doc_id": r["doc_id"], "department": r["doc_id"], "label": r["label"]} for r in answered]
        return Turn("clarify", "Ответ зависит от дирекции. Какая дирекция ведёт проект?",
                    options=opts + [{"doc_id": "*", "label": "Проект нескольких дирекций"}, {"doc_id": "?", "label": "Не знаю"}],
                    trace=tr, hits=hits)
    return _finish(answered or results, note)
