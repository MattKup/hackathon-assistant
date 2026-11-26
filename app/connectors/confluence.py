"""Коннектор Confluence (без аддонов — встроенный REST API) → тот же путь, что загрузка документа.

Страница пространства → тело в формате storage (XHTML) → разборщик app/parsers/html_simple.py → нарезка по
заголовкам → индекс. Подразделение — из сопоставления «пространство → подразделение» (config/documents.json,
«confluence_spaces») или из аргумента. Инкрементально: номер версии страницы хранится; изменилась — старая версия
снимается с индекса, новая загружается; удалённые из пространства — снимаются.

API (сверено по документации Atlassian):
- Cloud v2: GET {base}/wiki/api/v2/spaces?keys=KEY → id; GET /wiki/api/v2/spaces/{id}/pages?body-format=storage
  (results[].id, title, version.number, body.storage.value; следующая страница — _links.next);
- Server/Data Center: GET {base}/rest/api/content?spaceKey=KEY&type=page&expand=body.storage,version&start=&limit=.
Авторизация: Cloud — basic «email:API-токен», Server/DC — Bearer (персональный токен). Токен — из файла, не из кода
и не в лог.
"""
import base64
import json
import sqlite3
import time
import urllib.parse
import urllib.request

SCHEMA = "create table if not exists confluence_pages(page_id text primary key, space text, title text, version integer, doc_id text, synced real)"


class Confluence:
    def __init__(self, base_url, token, email="", cloud=None, fetch=None):
        self.base = base_url.rstrip("/")
        self.cloud = ("atlassian.net" in self.base) if cloud is None else cloud
        if self.cloud:
            self.auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        else:
            self.auth = f"Bearer {token}"
        self.fetch = fetch or self._fetch

    def _fetch(self, url):
        req = urllib.request.Request(url, headers={"Authorization": self.auth, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def pages(self, space_key, limit=50):
        """[(page_id, title, version, storage_html)] всех страниц пространства."""
        out = []
        if self.cloud:
            sp = self.fetch(f"{self.base}/wiki/api/v2/spaces?keys={urllib.parse.quote(space_key)}")["results"]
            if not sp:
                raise ValueError(f"пространство {space_key} не найдено")
            url = f"{self.base}/wiki/api/v2/spaces/{sp[0]['id']}/pages?body-format=storage&limit={limit}"
            while url:
                js = self.fetch(url)
                for p in js.get("results", []):
                    out.append((str(p["id"]), p.get("title", ""), (p.get("version") or {}).get("number", 0),
                                ((p.get("body") or {}).get("storage") or {}).get("value", "")))
                nxt = (js.get("_links") or {}).get("next")
                url = (self.base + nxt if nxt and nxt.startswith("/") else nxt) if nxt else None
        else:
            start = 0
            while True:
                js = self.fetch(f"{self.base}/rest/api/content?spaceKey={urllib.parse.quote(space_key)}&type=page"
                                f"&expand=body.storage,version&start={start}&limit={limit}")
                res = js.get("results", [])
                for p in res:
                    out.append((str(p["id"]), p.get("title", ""), (p.get("version") or {}).get("number", 0),
                                ((p.get("body") or {}).get("storage") or {}).get("value", "")))
                if len(res) < limit:
                    break
                start += limit
        return out


def sync(conn, space_key, department, stage, upload, remove, db_path):
    """Инкрементальная синхронизация. upload(file_name, bytes, department, stage) → doc_id; remove(doc_id).
    Возвращает счётчики: новых, обновлённых, без изменений, снятых."""
    db = sqlite3.connect(db_path)
    db.execute(SCHEMA)
    seen, stats = set(), {"new": 0, "updated": 0, "same": 0, "removed": 0}
    for pid, title, ver, body in conn.pages(space_key):
        seen.add(pid)
        row = db.execute("select version, doc_id from confluence_pages where page_id=?", (pid,)).fetchone()
        if row and row[0] == ver:
            stats["same"] += 1
            continue
        html = f"<!-- confluence:{space_key}:{pid}:v{ver} --><h1>{title}</h1>{body}"
        doc_id = upload(f"{title}.html", html.encode(), department, stage)
        if row and row[1] and row[1] != doc_id:
            remove(row[1])
        db.execute("insert or replace into confluence_pages values(?,?,?,?,?,?)", (pid, space_key, title, ver, doc_id, time.time()))
        stats["updated" if row else "new"] += 1
    for pid, doc_id in db.execute("select page_id, doc_id from confluence_pages where space=?", (space_key,)).fetchall():
        if pid not in seen:
            remove(doc_id)
            db.execute("delete from confluence_pages where page_id=?", (pid,))
            stats["removed"] += 1
    db.commit()
    return stats
