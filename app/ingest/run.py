"""Приём корпуса через doc-intel (S1, минимальная версия для старта 26.11).

Корпус: ~/hackathon/inbox/corpus-<дата> на Mac mini (не в git).
Разборы: data/ir/<sha256>.json в клоне решения (не в git).
Манифест: data/ingest-manifest.jsonl. В RUN_DIR/metrics.json — только счётчики.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
INBOX = Path.home() / "hackathon" / "inbox" / "corpus-2026-11-26"
DI = os.environ.get("DOC_INTEL_URL", "").rstrip("/")
TOKEN = os.environ.get("DOC_INTEL_API_TOKEN", "")
RUN_DIR = Path(os.environ.get("RUN_DIR", "."))
DRIVE = "https://drive.google.com/uc?export=download&id="


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def api(method, path, body=None, headers=None, timeout=120):
    req = urllib.request.Request(DI + path, data=body, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:300].decode("utf-8", "replace")}


def download(file_id, expect_sha):
    """Скачивает файл по id; имя берёт из Content-Disposition. Возвращает путь."""
    INBOX.mkdir(parents=True, exist_ok=True)
    for p in INBOX.iterdir():
        if p.is_file() and sha256(p) == expect_sha:
            return p
    with urllib.request.urlopen(DRIVE + file_id, timeout=120) as r:
        cd = r.headers.get("Content-Disposition", "")
        name = None
        if "filename*=UTF-8''" in cd:
            name = urllib.parse.unquote(cd.split("filename*=UTF-8''", 1)[1].split(";")[0])
        elif 'filename="' in cd:
            raw = cd.split('filename="', 1)[1].split('"', 1)[0]
            try:
                name = raw.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                name = raw
        name = name or f"{file_id}.bin"
        data = r.read()
    dst = INBOX / Path(name).name
    dst.write_bytes(data)
    got = sha256(dst)
    if got != expect_sha:
        dst.unlink()
        raise RuntimeError(f"sha256 не совпал для {file_id}: {got[:12]} != {expect_sha[:12]}")
    return dst


def multipart(path, fields):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    fname = urllib.parse.quote(path.name)
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                  f"filename*=UTF-8''{fname}; filename=\"upload{path.suffix}\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n").encode())
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def submit(path, sha):
    body, ctype = multipart(path, {"backend": "vision"})
    code, js = api("POST", "/v1/documents", body,
                   {"Content-Type": ctype, "Idempotency-Key": f"hackathon-2026-11:{sha}"})
    if code not in (200, 201, 202):
        raise RuntimeError(f"POST /v1/documents {code}: {json.dumps(js, ensure_ascii=False)[:300]}")
    return js.get("job_id") or js.get("jobId") or js.get("id"), js


def wait(job_id, timeout_s=1800):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        code, js = api("GET", f"/v1/jobs/{job_id}")
        st = js.get("status")
        if st in ("done", "failed", "error", "cancelled"):
            return st, js
        time.sleep(5)
    return "timeout", {}


def summarize(ir):
    """Счётчики без текста: страницы, блоки, знаки, таблицы, качество страниц."""
    pages = ir.get("pages") or []
    blocks = chars = tables = 0
    quality = {}
    for pg in pages:
        q = pg.get("quality") or pg.get("decision") or pg.get("page_quality") or "?"
        if isinstance(q, dict):
            q = q.get("decision") or q.get("label") or "?"
        quality[str(q)] = quality.get(str(q), 0) + 1
        for b in pg.get("blocks") or []:
            blocks += 1
            chars += len(b.get("text") or "")
            if (b.get("type") or b.get("kind")) == "table":
                tables += 1
    return {"pages": len(pages), "blocks": blocks, "chars": chars, "table_blocks": tables,
            "pages_by_quality": quality, "top_keys": sorted(ir.keys())[:20]}


def main(argv):
    if not DI or not TOKEN:
        sys.exit("нет DOC_INTEL_URL или DOC_INTEL_API_TOKEN")
    (DATA / "ir").mkdir(parents=True, exist_ok=True)
    manifest = DATA / "ingest-manifest.jsonl"
    metrics = {"docs": [], "ok": 0, "failed": 0, "doc_intel": DI.rsplit(":", 1)[-1]}
    for arg in argv:
        file_id, expect = arg.split(":", 1)
        t0 = time.time()
        row = {"drive_id": file_id, "sha256": expect, "parser": "docintel"}
        try:
            path = download(file_id, expect)
            row["file_name"] = path.name
            row["bytes"] = path.stat().st_size
            ir_path = DATA / "ir" / f"{expect}.json"
            if ir_path.exists():
                row["outcome"] = "duplicate"
                ir = json.loads(ir_path.read_text())
            else:
                job_id, sub = submit(path, expect)
                row["job_id"] = job_id
                log(f"{path.name}: job {job_id}")
                st, js = wait(job_id)
                row["status"] = st
                if st != "done":
                    raise RuntimeError(f"job {job_id} {st}: {json.dumps(js.get('error'), ensure_ascii=False)[:300]}")
                code, ir = api("GET", f"/v1/jobs/{job_id}/result?sections=true&includeProperties=true")
                if code != 200:
                    raise RuntimeError(f"result {code}")
                ir_path.write_text(json.dumps(ir, ensure_ascii=False))
                row["outcome"] = "accepted"
                row["provenance"] = {k: js.get(k) for k in ("backend", "fingerprint", "image", "run_id") if k in js}
            row.update(summarize(ir))
            metrics["ok"] += 1
        except Exception as e:  # сбой одного файла не останавливает пачку
            row["outcome"] = "error"
            row["reason"] = str(e)[:400]
            metrics["failed"] += 1
            log(f"ОШИБКА {file_id}: {e}")
        row["seconds"] = round(time.time() - t0, 1)
        with open(manifest, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        metrics["docs"].append(row)
        log(f"{row.get('file_name', file_id)}: {row['outcome']} blocks={row.get('blocks')} chars={row.get('chars')}")
    (RUN_DIR / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0 if metrics["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
