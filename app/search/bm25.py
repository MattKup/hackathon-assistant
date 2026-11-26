"""BM25 по окнам с лёгкой русской нормализацией (без зависимостей).

Стемминг грубый: срезаем частые окончания, чтобы «договора/договору/договоров» совпали.
Плотный поиск (bge-m3) и реранкер добавляются поверх этой выдачи (app/search/hybrid.py).
"""
import math
import re
from collections import Counter

TOKEN = re.compile(r"[a-zа-яё0-9]+", re.I)
ENDINGS = sorted("""ами ями ого его ому ему ыми ими ой ей ий ый ая яя ое ее ые ие ую юю ов ев ам ям ах ях ом ем
ия ья ие ье ию ью ии ьи ей ою ею а я о е ы и у ю ь ть ться ет ут ют ит ат ят ешь ишь ем им ете ите ала ала ило
ыла ила ли ла ло на ны но""".split(), key=len, reverse=True)
STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот
от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам
ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз
тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее
сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про
всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более
всегда конечно всю между это мне нам который которые которая которого""".split())


def stem(w):
    w = w.lower().replace("ё", "е")
    if len(w) <= 4 or w.isdigit():
        return w
    for e in ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 4:
            return w[: -len(e)]
    return w


def tokens(text):
    return [stem(t) for t in TOKEN.findall(text) if t.lower() not in STOP]


class BM25:
    def __init__(self, windows, k1=1.4, b=0.6):
        self.windows = windows
        self.docs = [tokens(w["path"] + "\n" + w["text"]) for w in windows]
        self.k1, self.b = k1, b
        self.avg = sum(map(len, self.docs)) / max(1, len(self.docs))
        df = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.tf = [Counter(d) for d in self.docs]

    def search(self, query, k=20, allowed=None):
        q = tokens(query)
        scores = []
        for i, tf in enumerate(self.tf):
            if allowed is not None and self.windows[i]["doc_id"] not in allowed:
                continue
            dl = len(self.docs[i])
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg))
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        return [{**self.windows[i], "score": round(s, 4)} for s, i in scores[:k]]
