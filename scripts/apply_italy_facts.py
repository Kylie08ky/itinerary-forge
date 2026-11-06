# ruff: noqa: E501  （数据脚本：长句是文案，不折行）
"""把 data/italy/facts.json 里的官网事实核对一遍，通过的写进 data/city-overlays/<城市>.json（规格 12）。

核对规则（全部由脚本做，不靠人看）：
- 每条事实带「原文摘录 + 网址」；脚本自己去打开网址，**摘录必须在网页文字里逐字找到**（忽略大小写、空白、弯引号、破折号写法）。
- 一条事实的所有摘录都找到了才算通过；网址打不开（反爬、超时、证书错误）、摘录找不到，这条**不写**，列在报告里，
  页面上就写「出发前请到官网确认」，不替官网下断言。
- 通过的事实合并成这个地点的精校（键是维基数据编号），带来源网址、核对日期、核对了什么；`official_url` 只有页面打得开才写。
- 报告里给每条通过的摘录前后各 60 个字符，方便一眼看出上下文有没有被断章取义。

用法：
    python scripts/apply_italy_facts.py --dry-run          # 只核对、打印报告
    python scripts/apply_italy_facts.py                    # 核对并写入精校文件
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "data" / "italy" / "facts.json"
OVERLAYS = ROOT / "data" / "city-overlays"
UA = "ai-travel-planner-agent-facts/0.1 (personal project; checks opening-hours pages)"


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def norm(text: str) -> str:
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[‘’‛´`]", "'", text)
    text = re.sub(r"[“”„]", '"', text)
    text = re.sub(r"[–—‑−]", "-", text)
    text = re.sub(r"[*_#>]", "", text)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return re.sub(r"\s+([:;,.)])", r"\1", text)             # 网页常在冒号前多一个空格（「Giorni di chiusura : 25 dicembre」）


def page_text(client: httpx.Client, url: str, cache: dict[str, str | None]) -> str | None:
    if url not in cache:
        try:
            r = client.get(url, timeout=40)
            if r.status_code >= 400:
                cache[url] = None
            else:
                p = _Text()
                p.feed(r.text)
                cache[url] = norm(" ".join(p.parts))
        except (httpx.HTTPError, ValueError):
            cache[url] = None
    return cache[url]


def check_fact(client: httpx.Client, fact: dict, cache: dict[str, str | None]) -> tuple[bool, list[str]]:
    notes: list[str] = []
    for q in fact["quotes"]:
        text = page_text(client, q["url"], cache)
        if text is None:
            return False, [f"打不开：{q['url']}"]
        needle = norm(q["text"])
        at = text.find(needle)
        if at < 0:
            return False, [f"网页里找不到原文：「{q['text'][:70]}」"]
        notes.append("…" + text[max(0, at - 60): at + len(needle) + 60] + "…")
    return True, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--today", default=date.today().isoformat())
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    data = json.loads(FACTS.read_text(encoding="utf-8"))
    cache: dict[str, str | None] = {}
    passed = failed = 0
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        for city in data["cities"]:
            overlay_path = OVERLAYS / f"{city['id']}.json"
            doc = (json.loads(overlay_path.read_text(encoding="utf-8")) if overlay_path.exists()
                   else {"city": city["id"], "_note": "精校：对着官方页面核对过的事实（规格 10 / 12）。键是维基数据编号；每条必须带来源和核对日期。重新生成草稿不会丢。", "places": {}})
            print(f"== {city['id']}")
            for entry in city["entries"]:
                merged: dict = {}
                sources: list[dict] = []
                for fact in entry["facts"]:
                    ok, notes = check_fact(client, fact, cache)
                    mark = "✓" if ok else "✗"
                    print(f"  {mark} {entry['name']}：{fact['what']}")
                    for n in notes:
                        print(f"      {n}")
                    if not ok:
                        failed += 1
                        continue
                    passed += 1
                    for key, value in fact["set"].items():
                        if key in ("closed_dates",):
                            merged[key] = sorted(set(merged.get(key, [])) | set(value))
                        elif key == "date_notes":
                            merged[key] = {**merged.get(key, {}), **value}
                        else:
                            merged[key] = value
                    sources.append({"url": fact["quotes"][0]["url"], "checked_at": args.today, "what": fact["what"]})
                if "closed_dates" in merged or "date_notes" in merged:
                    merged["hours_verified"] = True          # 官网原文写了 12 月 / 1 月的具体日期，核对过；只有每周闭馆日的不算（圣诞怎么开还没核对）
                if entry.get("official_url") and sources and page_text(client, entry["official_url"], cache) is not None:
                    merged["official_url"] = entry["official_url"]
                if merged and sources:
                    doc["places"][entry["qid"]] = {**merged, "sources": sources}
            if not args.dry_run:
                overlay_path.parent.mkdir(parents=True, exist_ok=True)
                overlay_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n通过 {passed} 条，没通过 {failed} 条（没通过的不写，页面写「出发前请到官网确认」）。{'（dry-run，没写文件）' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
