"""Коннектор Confluence на записанных ответах API (своего Confluence нет): Cloud v2 и Server, инкрементальная синхронизация."""
import tempfile
import unittest
from pathlib import Path

from app.chunking.clauses import build
from app.connectors.confluence import Confluence, sync
from app.parsers.html_simple import parse_html

PAGE = '<h2>Согласование</h2><p>Договор согласует юрист за два рабочих дня.</p>'


def cloud_api(pages):
    def fetch(url):
        if "/spaces?keys=" in url:
            return {"results": [{"id": "42", "key": "PROC"}]}
        if "/spaces/42/pages" in url and "cursor" not in url:
            return {"results": pages[:1], "_links": {"next": "/wiki/api/v2/spaces/42/pages?cursor=abc"}}
        if "cursor=abc" in url:
            return {"results": pages[1:], "_links": {}}
        raise AssertionError(url)
    return fetch


def page(pid, ver, body=PAGE, title="Регламент закупок"):
    return {"id": pid, "title": title, "version": {"number": ver}, "body": {"storage": {"value": body}}}


class ConfluenceTest(unittest.TestCase):
    def run_sync(self, pages, db):
        up, rm = [], []
        conn = Confluence("https://acme.atlassian.net", "tok", "a@b.c", fetch=cloud_api(pages))
        st = sync(conn, "PROC", "LAW", "full_cycle", lambda n, d, dep, stg: (up.append((n, d)), f"doc-{n}-{len(d)}")[1], rm.append, db)
        return st, up, rm

    def test_cloud_pagination_and_incremental(self):
        db = str(Path(tempfile.mkdtemp()) / "c.sqlite")
        st, up, _ = self.run_sync([page("1", 1), page("2", 3, title="Сроки")], db)
        self.assertEqual(st["new"], 2)
        self.assertIn("<h2>Согласование</h2>", up[0][1].decode())
        st, up, rm = self.run_sync([page("1", 1), page("2", 4, body=PAGE + "<p>Новый абзац.</p>", title="Сроки")], db)
        self.assertEqual((st["same"], st["updated"], len(rm)), (1, 1, 1))      # обновлённая страница заменила старую
        st, up, rm = self.run_sync([page("1", 1)], db)
        self.assertEqual(st["removed"], 1)                                     # удалённая из пространства — снята

    def test_server_api(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return {"results": [page("7", 2)]}
        conn = Confluence("https://wiki.company.local", "pat", fetch=fetch)
        self.assertFalse(conn.cloud)
        self.assertEqual(conn.auth, "Bearer pat")
        self.assertEqual(conn.pages("PROC")[0][:3], ("7", "Регламент закупок", 2))
        self.assertIn("/rest/api/content?spaceKey=PROC", calls[0])

    def test_page_html_to_windows(self):
        html = f"<h1>Регламент закупок</h1>{PAGE}"
        ws = build({"doc_id": "x", "title": "Регламент закупок", "department": "LAW", "stage": "full_cycle"}, parse_html(html))
        self.assertTrue(any(w.section_path[-1] == "Согласование" and "юрист" in w.text for w in ws))


if __name__ == "__main__":
    unittest.main()
