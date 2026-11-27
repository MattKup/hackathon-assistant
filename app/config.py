"""Подразделения и предметная область — из config/documents.json, без кода (универсальная версия, 26.11).

Новый документ нового подразделения (загрузка жюри, коннектор) работает без правки кода: название берётся из
department_titles, иначе из реестра (department_name), синоним — само название. Поведение на 4 документах хакатона
то же, что было с константами в коде.
"""
import json
import re
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config" / "documents.json"
_CFG = None


_OVR_PATH = None      # data/settings.json — настройки, изменённые службой качества в вебе (поверх config/documents.json)


def cfg():
    global _CFG
    if _CFG is None:
        _CFG = json.loads(CONFIG.read_text())
        if _OVR_PATH and Path(_OVR_PATH).exists():
            _CFG.update(json.loads(Path(_OVR_PATH).read_text()))
    return _CFG


EDITABLE = ("qa_topics", "repeat_monitor", "aspects")


def use_overrides(path):
    """Сервер указывает файл настроек в своей папке данных; конфигурация перечитывается."""
    global _OVR_PATH, _CFG
    _OVR_PATH, _CFG = str(path), None


def save_overrides(changes):
    """Сохранить изменённые настройки (только разрешённые ключи) и применить сразу, без перезапуска."""
    global _CFG
    if not _OVR_PATH:
        raise RuntimeError("файл настроек не задан")
    cur = json.loads(Path(_OVR_PATH).read_text()) if Path(_OVR_PATH).exists() else {}
    cur.update({k: v for k, v in changes.items() if k in EDITABLE})
    Path(_OVR_PATH).write_text(json.dumps(cur, ensure_ascii=False, indent=2))
    _CFG = None
    return {k: cfg().get(k) for k in EDITABLE}


def dept_ids(registry):
    """Подразделения в порядке конфигурации, затем новые из реестра."""
    known = list(cfg().get("department_titles") or cfg().get("departments") or {})
    extra = sorted({d["department"] for d in registry} - set(known))
    present = {d["department"] for d in registry}
    return [x for x in known if x in present or not registry] + extra


def titles(registry):
    t = dict(cfg().get("department_titles") or cfg().get("departments") or {})
    for d in registry:
        t.setdefault(d["department"], d.get("department_name") or d["department"])
    return t


def aliases(registry):
    a = {k: list(v) for k, v in (cfg().get("department_aliases") or {}).items()}
    for dep, title in titles(registry).items():
        a.setdefault(dep, [re.escape(title.lower())])
    return a


def domain():
    return cfg().get("domain") or "внутренние регламенты компании"


def qa_topics():
    """Темы, которые разбирает служба качества, хотя их нет в базе регламентов (настройка компании, а не код):
    вопрос сотрудника на такую тему — письмом в СК, а не «вне регламентов»."""
    return [t for t in cfg().get("qa_topics") or [] if t]
