"""Кэш ответов между пользователями (универсальная версия, 26.11).

Ключ — нормализованный вопрос + подразделение + набор окон контекста + версия индекса. Совпадение только точное
(семантический кэш по похожим вопросам может выдать чужой ответ — против кейса 1). Версия индекса меняется при
любой пересборке (загрузка документа, публикация или отзыв практики) — кэш сбрасывается, устаревший ответ не выдаётся.
Повторные вопросы — главная боль службы качества («отвечаем снова и снова»): на масштабе частые вопросы отвечаются
без вызова модели; для остальных работает префиксный кэш vLLM (статичная часть промпта — первой).
"""
import hashlib
import re
import threading
from collections import OrderedDict


def norm_q(q):
    q = q.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w%+]+", " ", q)).strip()


class AnswerCache:
    def __init__(self, size=2000):
        self.size, self.version, self.lock = size, 0, threading.Lock()
        self.d, self.hits, self.misses = OrderedDict(), 0, 0

    def key(self, *parts):
        return hashlib.sha1(repr((self.version,) + parts).encode()).hexdigest()

    def get(self, k):
        with self.lock:
            if k in self.d:
                self.d.move_to_end(k)
                self.hits += 1
                return self.d[k]
            self.misses += 1
            return None

    def put(self, k, v):
        with self.lock:
            self.d[k] = v
            self.d.move_to_end(k)
            while len(self.d) > self.size:
                self.d.popitem(last=False)

    def invalidate(self):
        with self.lock:
            self.version += 1
            self.d.clear()

    def stats(self):
        n = self.hits + self.misses
        return {"entries": len(self.d), "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / n, 3) if n else None, "version": self.version}
