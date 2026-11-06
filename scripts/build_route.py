"""批量核对并写入深度路线（规格 09）：写好路线提纲 → 一条命令核对所有来源和数字 → 通过才写进 data/routes/<城市>.json。

用法：
    python scripts/build_route.py data/routes/drafts/paris-fashion.json            # 核对并写入（同 id 的路线被替换）
    python scripts/build_route.py data/routes/drafts/*.json                        # 一次处理多条路线（批量）
    python scripts/build_route.py --check-only                                     # 只核对已经写进去的所有路线
    python scripts/build_route.py <提纲> --dry-run                                  # 只核对，不写文件

提纲的格式 = 最终的路线文件里的一条路线（见 app/routes.py），区别只有：`checked_at` 可以不写（自动填今天）、
`look_for` 可以不写（自动取地点库里这个地点的 highlights）。

脚本替你做的（以前我是一个页面一个页面手工核的）：
1. **来源能打开**：每个来源网址都真的请求一次（维基百科条目取全文，其他网址取页面文字）。
2. **数字有出处**：故事、看点和理由里的年份 / 数量（3 位以上的数字），必须在这一章或这一站自己引用的来源里出现；
   对不上就报错、不写文件（防止「凭印象写的年份」混进去）。
3. **延伸阅读真实存在**：来源页面里必须同时出现作者姓和年份。
4. **地点对得上**：地点都在地点库里、没有长期闭馆、没有重复；同一章里站的先后（after）合法。
5. 通过后再用 `load_routes` 完整校验一遍（和运行时同一套规则）。

它**核对不了**的（会在报告里提醒，需要人看）：事实表述有没有被曲解（脚本只查数字和来源能不能打开，不理解句子）、
地点的官网开放时间和票价（那是地点库的事，见 scripts/add_route_places.py）。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.library import load_library  # noqa: E402
from app.routes import Route, RouteError, RoutesFile, validate_against_library  # noqa: E402

UA = "ai-travel-planner-agent-routes/0.1 (https://github.com/Kylie08ky; personal project)"
DATA = ROOT / "data" / "routes"
NUM = re.compile(r"(?<![\d.])(\d{3,4})(?![\d])")           # 年份和 100 以上的数量；「5000 多」「1656–1659」都拆成单个数
_cache: dict[str, str] = {}


def fetch_text(client: httpx.Client, url: str) -> str:
    """来源的正文：维基百科取全文纯文本，其他网址取页面文字。失败抛 RuntimeError。"""
    if url in _cache:
        return _cache[url]
    u = urlparse(url)
    if u.netloc.endswith("wikipedia.org") and u.path.startswith("/wiki/"):
        title = unquote(u.path[len("/wiki/"):])
        r = client.get(f"https://{u.netloc}/w/api.php", params={
            "action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1, "format": "json",
            "formatversion": 2, "titles": title}, timeout=40)
        r.raise_for_status()
        pages = r.json()["query"]["pages"]
        text = pages[0].get("extract") or ""
        if pages[0].get("missing") or not text:
            raise RuntimeError(f"维基百科没有这个条目：{title}")
    else:
        r = client.get(url, timeout=40, follow_redirects=True)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        text = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", " ", r.text)))
    text = text.replace(",", "")                          # 5,000 → 5000
    _cache[url] = text
    return text


def numbers(text: str) -> set[str]:
    return set(NUM.findall(text.replace(",", "")))


def ground(label: str, claims: list[str], source_texts: list[str], problems: list[str],
           names: list[str] | None = None, explain: list[str] | None = None) -> None:
    """claims 里的每个数字都要在 source_texts 里出现；explain 收集「数字 ← 哪几个来源」，给人复核用。"""
    have = set().union(*(numbers(t) for t in source_texts)) if source_texts else set()
    for claim in claims:
        for n in sorted(numbers(claim)):
            if n not in have:
                problems.append(f"{label}：「{n}」在它引用的来源里找不到（出现在：{claim[:40]}…）")
            elif explain is not None:
                where = [names[i] for i, t in enumerate(source_texts) if names and n in numbers(t)]
                explain.append(f"{label}：{n} ← {'、'.join(where) or '来源'}")


def check_route(raw: dict, client: httpx.Client, today: str, lib,
                explain: list[str] | None = None) -> tuple[dict, list[str], list[str]]:
    """返回 (补全后的路线, 错误, 提醒)。"""
    problems: list[str] = []
    warnings: list[str] = []

    def fill_source(s: dict) -> None:
        s.setdefault("checked_at", today)

    for ch in raw.get("chapters", []):
        for s in ch.get("sources", []):
            fill_source(s)
        ch_texts: list[str] = []
        ch_names: list[str] = []
        for s in ch.get("sources", []):
            try:
                ch_texts.append(fetch_text(client, s["url"]))
                ch_names.append(s.get("title", s["url"]))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"章「{ch.get('title')}」的来源打不开 {s['url']}：{exc}")
        ground(f"章「{ch.get('title')}」故事", [ch.get("story", "")], ch_texts, problems, ch_names, explain)
        for st in ch.get("stops", []):
            pid = st.get("place_id", "")
            place = lib.places.get(pid)
            if place and not st.get("look_for") and st.get("priority", "core") == "core":
                st["look_for"] = list(place.highlights[:3])
                if not st["look_for"]:
                    warnings.append(f"{pid}：地点库里没有看点，core 站需要你写 look_for")
            for s in st.get("sources", []):
                fill_source(s)
            st_texts = list(ch_texts)                      # 站的依据 = 这一站自己的来源 + 本章的来源
            st_names = list(ch_names)
            for s in st.get("sources", []):
                try:
                    st_texts.append(fetch_text(client, s["url"]))
                    st_names.append(s.get("title", s["url"]))
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"站「{pid}」的来源打不开 {s['url']}：{exc}")
            ground(f"站「{pid}」", [st.get("why_here", ""), *st.get("look_for", [])], st_texts, problems,
                   st_names, explain)
            if place is not None and not place.hours_verified:
                warnings.append(f"{pid}：开放时间在地点库里没有核对过，页面会写「以官网为准」")
    for rd in raw.get("reading", []):
        src = rd.get("source", {})
        src.setdefault("checked_at", today)
        try:
            text = fetch_text(client, src["url"])
        except Exception as exc:  # noqa: BLE001
            problems.append(f"延伸阅读「{rd.get('title')}」的来源打不开 {src.get('url')}：{exc}")
            continue
        surname = re.split(r"\s+", rd.get("creator", "").strip())[-1] if rd.get("creator") else ""
        if surname and surname not in text:
            problems.append(f"延伸阅读「{rd.get('title')}」：来源页面里没有作者姓「{surname}」")
        if str(rd.get("year", "")) not in text:
            problems.append(f"延伸阅读「{rd.get('title')}」：来源页面里没有年份 {rd.get('year')}")
    return raw, problems, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outlines", nargs="*", help="路线提纲文件（可以多个）")
    ap.add_argument("--city", default="paris")
    ap.add_argument("--check-only", action="store_true", help="只核对已经写进去的路线")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--explain", action="store_true", help="列出每个数字出自哪几个来源，给人复核")
    args = ap.parse_args()
    today = date.today().isoformat()
    lib = load_library(args.city)
    path = DATA / f"{args.city}.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"city": args.city, "routes": []}
    todo: list[dict] = []
    if args.check_only:
        todo = current["routes"]
    for f in args.outlines:
        todo.append(json.loads(Path(f).read_text(encoding="utf-8")))
    if not todo:
        ap.error("给至少一个提纲文件，或者用 --check-only")

    failed = False
    done: dict[str, dict] = {}
    with httpx.Client(headers={"User-Agent": UA}) as client:
        for raw in todo:
            rid = raw.get("id", "?")
            explain: list[str] = []
            fixed, problems, warnings = check_route(raw, client, today, lib, explain)
            try:
                route = Route.model_validate(fixed)
                validate_against_library(route, lib)
            except (ValueError, RouteError) as exc:
                problems.append(f"路线数据不成立：{exc}")
            print(f"== {rid}：{len(problems)} 个错误，{len(warnings)} 条提醒")
            for p in problems:
                print("  ✗", p)
            for w in warnings:
                print("  ·", w)
            if args.explain:
                for line in explain:
                    print("  =", line)
            if problems:
                failed = True
            else:
                done[rid] = fixed
    if failed:
        print("有错误，没有写任何文件。")
        return 1
    if args.check_only or args.dry_run:
        print("核对通过（没写文件）。")
        return 0
    routes = [r for r in current["routes"] if r["id"] not in done] + list(done.values())
    RoutesFile.model_validate({"city": args.city, "routes": routes})
    text = json.dumps({"city": args.city, "routes": routes}, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")
    print(f"已写入 {path.relative_to(ROOT)}：{', '.join(done)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
