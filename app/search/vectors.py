"""Векторы окон на диске (универсальная версия, 26.11).

Ключ — хэш текста окна (путь + текст): перезапуск и пересборка индекса не пересчитывают векторы, одинаковые
тексты (редакции, копии) считаются один раз. На большой базе (сотни регламентов, десятки тысяч окон) это минуты
работы TEI при каждом старте, которых теперь нет. Файл — data/, не в git.
"""
import hashlib
import json
import threading


class VectorStore:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()

    @staticmethod
    def key(w):
        return hashlib.sha1((w["path"] + "\n" + w["text"]).encode()).hexdigest()

    def load(self):
        out = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    k, v = json.loads(line)
                    out[k] = v
        return out

    def add(self, vecs):
        with self.lock, open(self.path, "a") as fh:
            for k, v in vecs.items():
                fh.write(json.dumps([k, [round(x, 6) for x in v]]) + "\n")
