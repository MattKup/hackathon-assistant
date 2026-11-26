"""Загрузка документа службой качества: DOCX — простой разборщик; PDF/сканы/картинки — doc-intel (backend=vision).

Разбор → абзацы → нарезка по пунктам → окна добавляются в индекс. Статус хранится в SQLite.
"""
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path

from app.chunking.clauses import build
from app.parsers.docx_simple import Para, parse as parse_docx

SCHEMA = """create table if not exists uploads(
  doc_id text primary key, created real, file_name text, department text, stage text, status text,
  parser text, pages integer, ocr_pages integer, blocks integer, windows integer, chars integer, error text, job_id text)"""
EXT_OK = {".docx", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".html", ".htm"}


def parse_multipart(content_type, body):
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    fields, files = {}, {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        fname = part.get_filename()
        data = part.get_payload(decode=True) or b""
        if fname:
            files[name] = (fname, data)
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def ir_to_paras(ir):
    """IR doc-intel → абзацы для нарезки.

    С `?sections=true` (контракт DI5) у фрагментов `chunks[]` есть `sectionPath` (путь заголовков от корня,
    переходит через страницы) и `kind` (block / table / section). Берём block и table — свои окна 600–1000 знаков
    строит наша нарезка; склейки kind=section (до 4000 знаков) не берём. Канонические фрагменты есть только у страниц,
    прошедших контроль качества; страницы без фрагментов (на проверке) берём из pages[].blocks с пометкой review.
    Старый doc-intel без sectionPath → как раньше, по pages[].blocks (type=title — запасной заголовок).
    """
    out, covered = [], set()
    chunks = [c for c in ir.get("chunks") or [] if (c.get("kind") or "block") in ("block", "table") and (c.get("text") or "").strip()]
    if chunks and any(c.get("sectionPath") for c in chunks):
        for c in sorted(chunks, key=lambda c: c.get("page", 0)):        # порядок внутри страницы — как отдал doc-intel
            covered.add(c.get("page"))
            out.append(Para(len(out), c["text"].strip(), table=c.get("kind") == "table",
                            section_path=[x for x in (c.get("sectionPath") or []) if x], page=c.get("page", 0)))
    for p in ir.get("pages", []):
        if p.get("page") in covered:
            continue
        review = bool(covered)                                           # есть канонические фрагменты, а этой страницы в них нет
        for b in sorted(p.get("blocks", []), key=lambda b: b.get("readingIndex", 0)):
            t = (b.get("text") or "").strip()
            if not t:
                continue
            kind = (b.get("type") or "").lower()
            out.append(Para(len(out), t, is_list="list" in kind, bold=kind in ("title", "heading"), table=kind == "table",
                            level=1 if kind in ("title", "heading") else 0, page=p.get("page", 0), review=review))
    return out


class Uploads:
    def __init__(self, db_path, folder, docintel_url="", token_file=""):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.di = docintel_url.rstrip("/")
        self.token_file = token_file

    def rows(self):
        return [dict(r) for r in self.db.execute("select * from uploads order by created desc")]

    def _set(self, doc_id, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        self.db.execute(f"update uploads set {cols} where doc_id=?", (*kw.values(), doc_id))
        self.db.commit()

    def accept(self, file_name, data, department, stage):
        ext = Path(file_name).suffix.lower()
        if ext not in EXT_OK:
            raise ValueError(f"формат {ext or '?'} не поддерживается (DOCX, PDF, PNG, JPEG, TIFF)")
        if len(data) > 50 * 1024 * 1024:
            raise ValueError("файл больше 50 МБ")
        sha = hashlib.sha256(data).hexdigest()
        if self.db.execute("select 1 from uploads where doc_id=?", (sha,)).fetchone():
            return sha, False
        (self.folder / f"{sha}{ext}").write_bytes(data)
        self.db.execute("insert into uploads(doc_id, created, file_name, department, stage, status, parser) values(?,?,?,?,?,?,?)",
                        (sha, time.time(), file_name, department, stage, "processing", "docx_simple" if ext == ".docx" else ("html_simple" if ext in (".html", ".htm") else "doc-intel")))
        self.db.commit()
        return sha, True

    def process(self, doc_id):
        """Разобрать загруженный файл, вернуть (doc, windows) или бросить исключение (статус error)."""
        r = dict(self.db.execute("select * from uploads where doc_id=?", (doc_id,)).fetchone())
        path = next(self.folder.glob(f"{doc_id}.*"))
        try:
            if path.suffix.lower() == ".docx":
                paras, pages, ocr = parse_docx(path), 1, 0
            elif path.suffix.lower() in (".html", ".htm"):           # страница Confluence / выгрузка wiki
                from app.parsers.html_simple import parse_html
                paras, pages, ocr = parse_html(path.read_text(errors="replace")), 1, 0
            else:
                ir, job = self._docintel(path, doc_id)
                paras = ir_to_paras(ir)
                pages = len(ir.get("pages", []))
                ocr = sum(1 for p in ir.get("pages", []) if p.get("ocr"))
                self._set(doc_id, job_id=job)
            title = paras[0].text[:150] if paras else Path(r["file_name"]).stem
            cfg = json.loads((Path(__file__).resolve().parents[2] / "config" / "documents.json").read_text())
            doc = {"doc_id": doc_id, "file_name": r["file_name"], "title": title, "display_title": Path(r["file_name"]).stem,
                   "department": r["department"], "department_name": cfg["departments"].get(r["department"], r["department"]),
                   "stage": r["stage"], "status": "active", "uploaded": True,
                   "status_reason": "загружен службой качества", "parser": r["parser"], "paragraphs": len(paras),
                   "chars": sum(len(p.text) for p in paras)}
            ws = [w.to_dict() for w in build(doc, paras)]
            if not ws:
                raise ValueError("в документе не найдено текста")
            doc["windows"] = len(ws)
            (self.folder / f"{doc_id}.json").write_text(json.dumps({"doc": doc, "windows": ws}, ensure_ascii=False))
            self._set(doc_id, status="ready", pages=pages, ocr_pages=ocr, blocks=len(paras), windows=len(ws), chars=doc["chars"])
            return doc, ws
        except Exception as e:
            self._set(doc_id, status="error", error=str(e)[:300])
            raise

    def load_ready(self):
        out = []
        for r in self.rows():
            f = self.folder / f"{r['doc_id']}.json"
            if r["status"] == "ready" and f.exists():
                js = json.loads(f.read_text())
                out.append((js["doc"], js["windows"]))
        return out

    def remove(self, doc_id):
        self.db.execute("delete from uploads where doc_id=?", (doc_id,))
        self.db.commit()
        for f in self.folder.glob(f"{doc_id}*"):
            f.unlink()

    # --- doc-intel ---
    def _token(self):
        return Path(self.token_file).read_text().strip() if self.token_file and Path(self.token_file).exists() else ""

    def _api(self, method, path, body=None, headers=None, timeout=120):
        req = urllib.request.Request(self.di + path, data=body, method=method,
                                     headers={"Authorization": f"Bearer {self._token()}", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {"raw": e.read()[:300].decode("utf-8", "replace")}

    def _docintel(self, path, sha):
        if not self.di:
            raise RuntimeError("doc-intel не настроен (DOC_INTEL_URL)")
        boundary = uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="backend"\r\n\r\nvision\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="upload{path.suffix}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        code, js = self._api("POST", "/v1/documents", body, {"Content-Type": f"multipart/form-data; boundary={boundary}",
                                                              "Idempotency-Key": f"hackathon-upload:{sha}"})
        if code not in (200, 201, 202):
            raise RuntimeError(f"doc-intel {code}: {json.dumps(js, ensure_ascii=False)[:200]}")
        job = js.get("job_id") or js.get("jobId") or js.get("id")
        t0 = time.time()
        while time.time() - t0 < 1800:
            _, st = self._api("GET", f"/v1/jobs/{job}")
            if st.get("status") == "done":
                code, ir = self._api("GET", f"/v1/jobs/{job}/result?sections=true")
                if code != 200:
                    raise RuntimeError(f"doc-intel result {code}")
                return ir, job
            if st.get("status") in ("failed", "error", "cancelled"):
                raise RuntimeError(f"doc-intel: задание {st.get('status')}")
            time.sleep(3)
        raise RuntimeError("doc-intel: не дождались результата за 30 минут")
