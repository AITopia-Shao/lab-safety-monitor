"""
安全手册检索（轻量 RAG）
src/agent/rag.py

知识库：项目根目录 knowledge/*.md，按"## 标题"切成条款。
检索：中文字符二元组 + BM25，纯 Python，无需向量模型，香橙派上毫秒级。
"""
import glob
import math
import os
import re
from collections import Counter
from typing import List, Dict

from src.config import PROJECT_ROOT


def _tokens(text: str) -> List[str]:
    text = text.lower()
    out = []
    for seg in re.findall(r"[一-鿿]+|[a-z0-9]+", text):
        if re.match(r"[a-z0-9]", seg):
            out.append(seg)
        else:
            out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
            if len(seg) == 1:
                out.append(seg)
    return out


class Manual:
    def __init__(self, folder: str = None):
        self.folder = folder or os.path.join(PROJECT_ROOT, "knowledge")
        self.docs: List[Dict[str, str]] = []
        self._tf: List[Counter] = []
        self._df: Counter = Counter()
        self._avgdl = 1.0
        self.load()

    def load(self):
        self.docs.clear()
        for path in sorted(glob.glob(os.path.join(self.folder, "*.md"))):
            src = os.path.splitext(os.path.basename(path))[0]
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            for block in re.split(r"\n(?=## )", text):
                block = block.strip()
                if not block.startswith("## "):
                    continue
                title, _, body = block[3:].partition("\n")
                self.docs.append({"source": src, "title": title.strip(), "text": body.strip()})
        self._tf = [Counter(_tokens(d["title"] + " " + d["title"] + " " + d["text"])) for d in self.docs]
        self._df = Counter()
        for tf in self._tf:
            self._df.update(tf.keys())
        self._avgdl = (sum(sum(tf.values()) for tf in self._tf) / len(self._tf)) if self._tf else 1.0

    def search(self, query: str, k: int = 3) -> List[Dict[str, str]]:
        q = _tokens(query)
        if not q or not self.docs:
            return []
        n = len(self.docs)
        scores = []
        for i, tf in enumerate(self._tf):
            dl = sum(tf.values())
            s = 0.0
            for t in set(q):
                f = tf.get(t, 0)
                if not f:
                    continue
                idf = math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * dl / self._avgdl))
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        return [dict(self.docs[i], score=round(s, 2)) for s, i in scores[:k]]
