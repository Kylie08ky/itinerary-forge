"""笔记文本的小工具：折叠大小写 / 重音 / 空格后查找、按句切分、挑出像「指令」的句子。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.notes.models import SNIPPET_MAX

_LIGATURES = {"œ": "oe", "æ": "ae", "ß": "ss"}
_END = "。！？!?；;…"


def fold(text: str) -> str:
    """小写、去重音、去掉空格和标点，只留字母数字（中文原样）。「Café de Flore」和「cafe de flore」折成同一个。"""
    return _fold_with_index(text)[0]


def _fold_with_index(text: str) -> tuple[str, list[int]]:
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        ch = _LIGATURES.get(ch.lower(), ch)
        for c in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(c):
                continue
            c = c.casefold()
            for cc in c:
                if cc.isalnum():
                    out.append(cc)
                    idx.append(i)
    return "".join(out), idx


class Folded:
    """原文折叠后的样子 + 每个折叠字符对应原文的第几个字符，用来把命中位置换回原文位置。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.s, self.idx = _fold_with_index(text)

    def find_all(self, name: str) -> list[tuple[int, int]]:
        """name 折叠后在原文里的所有位置，返回原文里的 (起, 止)（止不含）。"""
        n = fold(name)
        if not n:
            return []
        spans: list[tuple[int, int]] = []
        start = 0
        while (at := self.s.find(n, start)) != -1:
            spans.append((self.idx[at], self.idx[at + len(n) - 1] + 1))
            start = at + len(n)
        return spans


@dataclass(frozen=True)
class Sentence:
    start: int
    end: int
    line: int           # 第几段：空行是硬边界，一句话的「后文」不跨段（单个换行不算：标题和说明常分在两行）

    def text_in(self, text: str) -> str:
        return text[self.start:self.end].strip()


_URL = re.compile(r"(?:官网\s*[:：]?\s*)?https?://\S+")  # 片段里不放网址（长、没用）
_SENT = re.compile(rf"[^{_END}\n]+[{_END}]*|\n")
_PARA_BREAK = re.compile(r"\n[ \t　]*\n")


def split_sentences(text: str) -> list[Sentence]:
    breaks = [m.end() for m in _PARA_BREAK.finditer(text)]
    out: list[Sentence] = []
    for m in _SENT.finditer(text):
        if not m.group().strip():
            continue
        out.append(Sentence(m.start(), m.end(), sum(1 for b in breaks if b <= m.start())))
    return out


def snippet_for(text: str, sentences: list[Sentence], starts: list[int], next_starts: set[int]) -> str:
    """一个地点的原文片段：它被提到的那一句，加上后面没有提到别的地点的句子（同一段内，空行分段）。

    `starts` 是它在原文里出现的位置；`next_starts` 是所有地点出现的位置（用来判断后一句是不是已经在说别的地点）。
    """
    parts: list[str] = []
    for pos in starts:
        k = next((i for i, s in enumerate(sentences) if s.start <= pos < s.end), None)
        if k is None:
            continue
        chunk = [sentences[k].text_in(text)]
        j = k + 1
        while j < len(sentences) and sentences[j].line == sentences[k].line and not any(
            sentences[j].start <= p < sentences[j].end for p in next_starts
        ):
            chunk.append(sentences[j].text_in(text))
            j += 1
        piece = _URL.sub("", " ".join(chunk)).strip()
        if piece and not any(piece in p for p in parts):        # 同一段被提到好几次时只留一份
            parts = [p for p in parts if p not in piece]
            parts.append(piece)
    return clip(" … ".join(parts))


def clip(s: str, limit: int = SNIPPET_MAX) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


# 像「对模型下命令」的句子：整句丢掉，不给扫描也不给模型看（笔记正文是不可信数据）
_INSTRUCTION = re.compile(
    r"忽略(以上|上面|前面|之前|所有|全部)|无视(以上|上面|前面|之前|所有|全部)|系统提示|提示词|"
    r"(输出|显示|告诉我|泄露).{0,6}(系统|提示词|指令|prompt)|你(现在)?是(一个|一名)?.{0,8}(助手|AI|模型)|"
    r"把(行程|计划).{0,6}(改成|换成)|"
    r"ignore (all |any )?(previous|above|prior)|system prompt|disregard|you are now|as an ai",
    re.IGNORECASE,
)


def drop_instructions(text: str) -> tuple[str, int]:
    """去掉像指令的句子，返回 (清理后的正文, 去掉了几句)。位置会变，所以先清理、再在清理后的正文上找地点。"""
    sentences = split_sentences(text)
    bad = [s for s in sentences if _INSTRUCTION.search(text[s.start:s.end])]
    if not bad:
        return text, 0
    keep: list[str] = []
    cur = 0
    for s in bad:
        keep.append(text[cur:s.start])
        cur = s.end
    keep.append(text[cur:])
    cleaned = "".join(keep)
    # 句子被整个删掉后可能留下空括号 / 孤立的括号，顺手清掉
    cleaned = re.sub(r"[（(]\s*[）)]", "", cleaned)
    return cleaned, len(bad)
