"""维基导游（Wikivoyage）与维基百科浏览量（规格 10 H21）。

两样都是开放数据：维基导游的内容是 CC BY-SA 4.0（要署名、同样方式共享），所以这里**只取名字、类别、坐标、
维基数据编号和官网**，不抄它的介绍文字；浏览量是维基媒体公开的统计接口。

纯函数（解析、取中文名、算浏览量）在上半部分，网络请求在下半部分；请求前后要停顿，User-Agent 写明用途。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

import httpx

VOYAGE_API = "https://{lang}.wikivoyage.org/w/api.php"
VOYAGE_PAGE = "https://{lang}.wikivoyage.org/wiki/{title}"
PAGEVIEWS = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{lang}.wikipedia/all-access/user/"
             "{title}/monthly/{start}/{end}")
KINDS = ("see", "do", "eat", "drink", "buy")
_CJK = re.compile(r"[一-鿿]+")
_QID = re.compile(r"Q\d+")


@dataclass(frozen=True)
class Listing:
    kind: str                 # see / do / eat / drink / buy
    name: str                 # 条目名（多半是英文 / 拼音）
    alt: str                  # 别名（多半含汉字）
    lat: float | None
    lng: float | None
    url: str
    wikidata: str             # Q 编号，没有则空
    lastedit: str             # 条目最后核对日期（可能为空）


def _templates(text: str) -> list[str]:
    """文本里所有最外层的 {{...}}（里面可以套 {{dead link}} 之类）。"""
    out: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(text) - 1:
        pair = text[i:i + 2]
        if pair == "{{":
            if depth == 0:
                start = i
            depth += 1
            i += 2
        elif pair == "}}" and depth:
            depth -= 1
            i += 2
            if depth == 0:
                out.append(text[start:i])
        else:
            i += 1
    return out


def _split_args(body: str) -> dict[str, str]:
    """「|a=1 |b=2」→ {a: 1, b: 2}；值里的 [[a|b]] 和嵌套模板里的竖线不当分隔。"""
    args: dict[str, str] = {}
    parts: list[str] = []
    depth = 0
    cur = ""
    i = 0
    while i < len(body):
        two = body[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            cur += two
            i += 2
        elif two in ("}}", "]]"):
            depth = max(depth - 1, 0)
            cur += two
            i += 2
        elif body[i] == "|" and depth == 0:
            parts.append(cur)
            cur = ""
            i += 1
        else:
            cur += body[i]
            i += 1
    parts.append(cur)
    for part in parts:
        key, eq, val = part.partition("=")
        if eq:
            args[key.strip().lower()] = val.strip()
    return args


def _num(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None


def parse_listings(wikitext: str) -> list[Listing]:
    """维基导游页面里的 {{see}} {{do}} {{eat}} {{drink}} {{buy}} 条目。没名字的丢掉。"""
    out: list[Listing] = []
    for tpl in _templates(wikitext):
        m = re.match(r"\{\{\s*(see|do|eat|drink|buy|listing)\b(.*)\}\}$", tpl, re.S | re.I)
        if not m:
            continue
        args = _split_args(m.group(2))
        kind = m.group(1).lower()
        if kind == "listing":
            kind = args.get("type", "").lower()
        if kind not in KINDS or not args.get("name"):
            continue
        qid = args.get("wikidata", "").strip()
        url = re.sub(r"\{\{.*?\}\}", "", args.get("url", "")).strip()
        out.append(Listing(kind, args["name"], args.get("alt", ""), _num(args.get("lat", "")),
                           _num(args.get("long", "")), url, qid if _QID.fullmatch(qid) else "",
                           args.get("lastedit", "")))
    return out


def chinese_name(item: Listing) -> str:
    """别名里的汉字名（「Tonghuai Temple (通淮关岳庙)」→ 通淮关岳庙）；别名没有就看条目名；都没有 → 空。"""
    for text in (item.alt, item.name):
        runs = _CJK.findall(text)
        if runs:
            return runs[0]
    return ""


def views_total(items: list[dict]) -> int:
    """月度浏览量接口返回的 items → 合计。"""
    return sum(int(i.get("views", 0)) for i in items)


def window(today: date) -> tuple[str, str]:
    """最近 12 个整月（不含当月）：→ (开始, 结束) 形如 20251101 / 20261001。"""
    end_y, end_m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    start_y, start_m = end_y - 1, end_m + 1
    if start_m == 13:
        start_y, start_m = end_y, 1
    return f"{start_y}{start_m:02d}01", f"{end_y}{end_m:02d}01"


# --------------------------------------------------------------------------- #
# 网络请求
# --------------------------------------------------------------------------- #
def fetch_voyage(client: httpx.Client, title: str, lang: str = "en", pause: float = 1.0) -> dict | None:
    """维基导游一座城市的页面 → {"listings": [...], "revid", "url", "lang"}；没有这一页 → None。"""
    try:
        r = client.get(VOYAGE_API.format(lang=lang), timeout=60, headers={"Accept": "application/json"},
                       params={"action": "parse", "page": title, "prop": "wikitext|revid", "format": "json",
                               "redirects": 1})
        time.sleep(pause)
        if r.status_code != 200:
            return None
        body = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    parsed = body.get("parse")
    if not parsed:
        return None
    return {"listings": parse_listings(parsed["wikitext"]["*"]), "revid": parsed["revid"], "lang": lang,
            "url": VOYAGE_PAGE.format(lang=lang, title=parsed["title"].replace(" ", "_"))}


def fetch_views(client: httpx.Client, title: str, today: date, lang: str = "zh", pause: float = 0.3) -> int | None:
    """某个维基百科条目最近 12 个整月的浏览量（只算真人）；条目不存在 / 取不到 → None。
    title 必须是维基数据里记的条目标题（改名重定向的标题会少算）。"""
    start, end = window(today)
    url = PAGEVIEWS.format(lang=lang, title=quote(title.replace(" ", "_"), safe=""),
                           start=start, end=end)
    try:
        r = client.get(url, timeout=30, headers={"Accept": "application/json"})
        time.sleep(pause)
        if r.status_code != 200:
            return None
        return views_total(r.json().get("items") or [])
    except (httpx.HTTPError, ValueError):
        return None
