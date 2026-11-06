"""把一段笔记正文解析成确认页要用的结果：库内地点（带置信度 / 片段 / 提示）+ 库外的待核实地点。

    正文 ─► 去掉像指令的句子 ─► 扫地点库名字 + 模型读库外名字 ─► 逐个对地点库 ─► 原文片段 ─► 规则提纯
"""

from __future__ import annotations

from collections.abc import Callable

from app.library import PlaceLibrary
from app.llm import LLMClient
from app.notes.extract import ModelFailed, ask_model, clean_name, find_spans, is_noise
from app.notes.fetch import FetchedPage
from app.notes.match import match_mention
from app.notes.models import (
    MAX_MENTIONS,
    MAX_NOTE_TEXT,
    Candidate,
    Confidence,
    ParsedPlace,
    ParseResult,
    PendingPlace,
)
from app.notes.text import Folded, drop_instructions, fold, snippet_for, split_sentences
from app.notes.tips import extract_tips, is_complaint

_RANK = {Confidence.low: 0, Confidence.medium: 1, Confidence.high: 2}


class NoteTooLong(ValueError):
    pass


def parse_url(
    url: str, lib: PlaceLibrary, client: LLMClient | None = None, *, fetcher: Callable[[str], FetchedPage],
) -> ParseResult:
    """读一个公开网页的文字再按笔记解析。读不到时 fetcher 抛 FetchError，由调用方告诉用户改贴正文。"""
    page = fetcher(url)
    return parse_text(page.text[:MAX_NOTE_TEXT], lib, client, source="url", source_host=page.host)


def parse_text(
    text: str, lib: PlaceLibrary, client: LLMClient | None = None, *,
    source: str = "text", source_host: str | None = None,
) -> ParseResult:
    text = text.strip()
    if len(text) > MAX_NOTE_TEXT:
        raise NoteTooLong(f"笔记正文最多 {MAX_NOTE_TEXT} 字（现在 {len(text)} 字）")
    cleaned, dropped = drop_instructions(text)
    result = ParseResult(dropped_instructions=dropped, source=source, source_host=source_host)
    if not cleaned.strip():
        return result

    model_names: list[str] = []
    if client is not None:
        try:
            model_names = ask_model(cleaned, client)
            result.model_used = True
        except ModelFailed:
            result.model_used = False

    folded = Folded(cleaned)
    spans = find_spans(folded, lib, model_names)
    sentences = split_sentences(cleaned)
    all_starts = {s.start for s in spans}

    # 地点 id（歧义时是一组 id）-> 出现位置、写法、最好的置信度
    found: dict[tuple[str, ...], dict] = {}
    pending: dict[str, dict] = {}
    for s in spans:
        surface = cleaned[s.start:s.end]
        m = match_mention(surface, lib, after=cleaned[s.end:s.end + 40])
        if m.kind == "skip":
            continue
        if m.kind == "unmatched":
            name = clean_name(surface)
            if is_noise(name) or s.source == "scan":
                continue
            slot = pending.setdefault(fold(name), {"name": name, "starts": []})
            slot["starts"].append(s.start)
            continue
        key = tuple(sorted(m.place_ids))
        slot = found.setdefault(key, {"mention": surface, "starts": [], "conf": m.confidence})
        slot["starts"].append(s.start)
        if _RANK[m.confidence] > _RANK[slot["conf"]]:
            slot["conf"], slot["mention"] = m.confidence, surface

    def snippet(starts: list[int]) -> str:
        return snippet_for(cleaned, sentences, sorted(starts), all_starts - set(starts))

    for key, slot in sorted(found.items(), key=lambda kv: min(kv[1]["starts"])):
        snip = snippet(slot["starts"])
        if len(key) == 1:
            place = lib.get_place(key[0])
            result.matched.append(ParsedPlace(
                mention=slot["mention"], place_id=place.id, name_zh=place.name_zh, area=place.area,
                confidence=slot["conf"], snippet=snip, tips=extract_tips(snip, place), complaint=is_complaint(snip),
                closed_long_term=place.long_term_closed,
            ))
        else:
            cands = [Candidate(place_id=pid, name_zh=lib.get_place(pid).name_zh, area=lib.get_place(pid).area)
                     for pid in sorted(key, key=lambda i: (len(lib.get_place(i).name_zh), i))]
            result.matched.append(ParsedPlace(
                mention=slot["mention"], confidence=Confidence.low, candidates=cands, snippet=snip,
                tips=extract_tips(snip), complaint=is_complaint(snip),
            ))
    for slot in sorted(pending.values(), key=lambda v: min(v["starts"])):
        result.pending.append(PendingPlace(name=slot["name"][:60], snippet=snippet(slot["starts"])))

    result.matched = result.matched[:MAX_MENTIONS]
    result.pending = result.pending[: max(0, MAX_MENTIONS - len(result.matched))]
    return result
