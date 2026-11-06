"""把一日游目的地数据里的繁体中文统一改成简体（规格 08 3.7，你的要求：全部显示简体）。

中文名和简介来自维基数据 / 中文维基百科，很多条目只有繁体。转换用中文维基百科自己的繁简转换
（MediaWiki `action=parse&variant=zh-hans`）：开放服务、不引入任何第三方包；只在开发时跑，结果写回 data/。

用法：
    python scripts/simplify_chinese.py --dry-run     # 只列出会改什么
    python scripts/simplify_chinese.py               # 写回 data/{cities,places,themes}/<一日游城市>.json

只处理一日游目的地（scripts/daytrip_destinations.py 里的城市）；巴黎等手工整理的数据不动。
转换结果里仍有「繁体独有字」会报错，不写回。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from apply_verification import dump  # noqa: E402
from daytrip_destinations import DESTINATIONS  # noqa: E402

API = "https://zh.wikipedia.org/w/api.php"
UA = "ai-travel-planner-agent-simplify/0.1 (https://github.com/Kylie08ky; personal project)"
CJK = re.compile(r"[一-鿿]")
P_TAG = re.compile(r"<p>(.*?)</p>", re.S)
DATA = ROOT / "data"
# 繁体转简体只转字，不转译名：台湾译名换成大陆通用译名，和城市数据里的写法一致
TERM_FIXES = {"史特拉斯堡": "斯特拉斯堡", "马斯垂克": "马斯特里赫特", "馬斯垂克": "马斯特里赫特", "烏爾姆": "乌尔姆",
              "乌姆": "乌尔姆", "法尔肯堡": "瓦尔肯堡"}


def _parse(client: httpx.Client, text: str) -> list[str]:
    """把一大段文字（段落之间空一行）交给中文维基百科转成简体，返回每个段落。遇到 429 退避重试。"""
    for wait in (0, 5, 15, 40, 90):
        time.sleep(wait)
        r = client.post(API, data={"action": "parse", "text": text, "contentmodel": "wikitext", "prop": "text",
                                   "variant": "zh-hans", "format": "json", "formatversion": 2,
                                   "disablelimitreport": 1}, timeout=60)
        if r.status_code == 429:
            continue
        r.raise_for_status()
        body = r.json()["parse"]["text"]
        return [html.unescape(re.sub(r"<[^>]+>", "", m)).strip() for m in P_TAG.findall(body)]
    raise RuntimeError("中文维基百科一直返回 429（请求太频繁），过一会儿再跑")


def to_simplified_all(client: httpx.Client, strings: list[str], batch: int = 12) -> dict[str, str]:
    """一批文字 → 简体，返回 {原文: 简体}。每批一个请求；段落数对不上就逐条转。转换前后长度差太多报错。"""
    out: dict[str, str] = {}
    for k in range(0, len(strings), batch):
        chunk = strings[k:k + batch]
        got = _parse(client, "\n\n".join(chunk))
        if len(got) != len(chunk):
            got = [(_parse(client, s) or [""])[0] for s in chunk]
        for src, res in zip(chunk, got, strict=True):
            if abs(len(res) - len(src.strip())) > max(3, len(src) // 10):
                raise ValueError(f"转换前后长度差太多（可能被当成维基标记）：{src[:40]!r} → {res[:40]!r}")
            out[src] = res
        time.sleep(1.0)
    return out


def cjk_strings(node, found: set[str]) -> None:
    """JSON 里所有含汉字的字符串。"""
    if isinstance(node, str):
        if CJK.search(node):
            found.add(node)
    elif isinstance(node, list):
        for x in node:
            cjk_strings(x, found)
    elif isinstance(node, dict):
        for v in node.values():
            cjk_strings(v, found)


def rewrite(node, table: dict[str, str]):
    if isinstance(node, str):
        return table.get(node, node)
    if isinstance(node, list):
        return [rewrite(x, table) for x in node]
    if isinstance(node, dict):
        return {k: rewrite(v, table) for k, v in node.items()}
    return node


def files(city_ids: list[str]) -> list[Path]:
    out = []
    for kind in ("cities", "places", "themes"):
        for cid in city_ids:
            p = DATA / kind / f"{cid}.json"
            if p.exists():
                out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    paths = files(sorted({d.id for d in DESTINATIONS}))
    docs = {p: json.loads(p.read_text(encoding="utf-8")) for p in paths}
    strings: set[str] = set()
    for d in docs.values():
        cjk_strings(d, strings)
    print(f"{len(paths)} 个文件，{len(strings)} 条含汉字的文字")
    with httpx.Client(headers={"User-Agent": UA}) as client:
        converted = to_simplified_all(client, sorted(strings))
    for src, res in converted.items():
        for wrong, right in TERM_FIXES.items():
            res = res.replace(wrong, right)
        converted[src] = res
    table = {s: t for s, t in converted.items() if t != s}
    print(f"其中 {len(table)} 条有繁体，要改")
    for s, out in list(table.items())[:40]:
        print(f"  {s[:40]}  →  {out[:40]}")
    if args.dry_run:
        return 0
    for p, d in docs.items():
        new = rewrite(d, table)
        if new != d:
            if p.parent.name == "places":              # 地点库一行一个点，和其他脚本写的格式一致
                dump(p, new)
            else:
                p.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("写回", p.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
