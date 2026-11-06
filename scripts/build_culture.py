"""批量核对并写入「文化入门」作品（规格 09 十三）：写好提纲 → 一条命令核对来源 → 通过才写进 data/culture/<城市>.json。

用法：
    python scripts/build_culture.py data/culture/drafts/paris.json            # 核对并写入（同 id 的作品被替换）
    python scripts/build_culture.py data/culture/drafts/paris.json --dry-run  # 只核对，不写文件
    python scripts/build_culture.py data/culture/drafts/paris.json --explain  # 同时列出每个地点在来源里出现的上下文，给人复核
    python scripts/build_culture.py data/culture/drafts/paris.json --suggest  # 列出来源里提到、但提纲没写的库内地点（挑选用）
    python scripts/build_culture.py --check-only                              # 只核对已经写进去的所有作品

提纲 = `{"city": "paris", "works": [...]}`，作品格式见 app/culture.py；`source.checked_at` 可以不写（自动填今天）。

脚本替你做的：
1. **来源能打开**：来源网址真的请求一次（维基百科取全文）。
2. **作品真实存在**：来源页面里必须同时有作者 / 导演的姓和年份。
3. **「带你看的地方」有出处**：每个地点（英文名 / 当地名 / 中文名的各种写法）必须在来源全文里出现。
4. **数字有出处**：简介和每条说明里的年份 / 数量（3 位以上的数字）必须在来源里出现。
5. 通过后再用 `load_culture` 完整校验（地点在库里、没有长期闭馆）。

它**核对不了**的：来源里「提到」这个地点，不等于「在这里拍的 / 写的」——说明里的措辞要人看。`--explain` 把来源里提到地点的那一句
列出来，我逐条对过再放行。
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.culture import MIN_POPULARITY, CultureError, CultureFile, load_culture, mentions, name_keys  # noqa: E402
from app.library import load_library  # noqa: E402
from app.names import fold  # noqa: E402
from app.notes.text import _fold_with_index  # noqa: E402
from scripts.build_route import UA, fetch_text, ground  # noqa: E402
from scripts.wikidata_util import entities, popularity, qid_for_url  # noqa: E402

DATA = ROOT / "data" / "culture"


def context(text: str, place, width: int = 70) -> str:
    """来源里第一次提到这个地点的那一段原文（折叠大小写 / 重音 / 标点后查找，再换回原文位置）。"""
    folded, idx = _fold_with_index(text)
    best = -1
    for key in name_keys(place):
        i = folded.find(key)
        if i != -1 and (best == -1 or i < best):
            best = i
    if best == -1:
        return ""
    pos = idx[best]
    return " ".join(text[max(0, pos - width):pos + width].split())


def zh_title_exists(client: httpx.Client, title: str) -> bool:
    """中文译名有据可查：中文维基百科有这个标题（含重定向，简繁转换后）。"""
    r = client.get("https://zh.wikipedia.org/w/api.php", params={
        "action": "query", "titles": title, "redirects": 1, "converttitles": 1, "format": "json",
        "formatversion": 2}, timeout=30)
    r.raise_for_status()
    pages = r.json()["query"]["pages"]
    return bool(pages) and not pages[0].get("missing", False)


def attach_fame(raw: dict, client: httpx.Client) -> None:
    """名气 = 来源条目有多少个语言版本的维基百科页面；顺便记下维基数据编号（自动收录去重用）。查不到就是 0。"""
    qid = qid_for_url(client, raw["source"]["url"])
    if qid:
        raw["wikidata"] = qid
        ent = entities(client, [qid], props="sitelinks").get(qid, {})
        raw["popularity"] = popularity(ent)
    else:
        raw.setdefault("popularity", 0)


def check_work(raw: dict, client: httpx.Client, today: str, lib, *, explain: bool = False,
               suggest: bool = False) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    out: list[str] = []
    src = raw.get("source", {})
    src.setdefault("checked_at", today)
    wid = raw.get("id", "?")
    try:
        text = fetch_text(client, src["url"])
    except Exception as exc:  # noqa: BLE001
        return [f"{wid}：来源打不开 {src.get('url')}：{exc}"], out
    folded = fold(text)
    surname = (raw.get("creator", "").split() or [""])[-1]
    if surname and fold(surname) not in folded:
        problems.append(f"{wid}：来源页面里没有作者 / 导演的姓「{surname}」")
    if str(raw.get("year", "")) not in text:
        problems.append(f"{wid}：来源页面里没有年份 {raw.get('year')}")
    names = [src.get("title", src["url"])]
    attach_fame(raw, client)
    if raw.get("popularity", 0) < MIN_POPULARITY:
        problems.append(f"{wid}：名气不够（维基百科只有 {raw.get('popularity', 0)} 个语言版本，门槛 {MIN_POPULARITY}），不收")
    zh = raw.get("title_zh", "")
    if zh and not zh_title_exists(client, zh):
        problems.append(f"{wid}：中文译名「{zh}」在中文维基百科里找不到（没核对过的译名不放进来）")
    ground(f"{wid} 简介", [raw.get("summary", "")], [text], problems, names)
    listed = set()
    for s in raw.get("sees", []):
        pid = s.get("place_id", "")
        listed.add(pid)
        p = lib.places.get(pid)
        if p is None:
            problems.append(f"{wid}：地点 {pid!r} 不在地点库里")
            continue
        extra = [m for m in s.get("match", []) if fold(m) in folded]          # 提纲里补充的写法，如「Shakespeare and Company」
        if not mentions(p, text) and not extra:
            problems.append(f"{wid}：来源里找不到地点「{p.name_zh}」（试过：{'、'.join(name_keys(p)[:4])}）")
        elif explain:
            out.append(f"  {pid}: …{context(text, p)}…")
        ground(f"{wid} 地点 {pid}", [s.get("note", "")], [text], problems, names)
    if suggest:
        extra = [p.id for p in lib.places.values() if p.id not in listed and not p.long_term_closed and mentions(p, text)]
        if extra:
            out.append(f"  来源里还提到（提纲没写）：{'、'.join(extra)}")
    return problems, out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("drafts", nargs="*", help="作品提纲文件")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--suggest", action="store_true")
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()
    today = date.today().isoformat()
    client = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True)

    jobs: list[tuple[str, list[dict]]] = []
    if args.check_only:
        for path in sorted(DATA.glob("*.json")):
            d = json.loads(path.read_text(encoding="utf-8"))
            jobs.append((d["city"], d["works"]))
    else:
        for f in args.drafts:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
            jobs.append((d["city"], d["works"]))
    if not jobs:
        ap.error("没有要核对的文件")

    failed = False
    for city, works in jobs:
        lib = load_library(city)
        for raw in works:
            problems, notes = check_work(raw, client, today, lib, explain=args.explain, suggest=args.suggest)
            mark = "✗" if problems else "✓"
            print(f"{mark} {raw.get('id')}  {raw.get('title')}")
            for line in notes:
                print(line)
            for pr in problems:
                print(f"    - {pr}")
            failed = failed or bool(problems)
        if failed or args.dry_run or args.check_only:
            continue
        path = DATA / f"{city}.json"
        existing = json.loads(path.read_text(encoding="utf-8"))["works"] if path.exists() else []
        by_id = {w["id"]: w for w in existing}
        for raw in works:
            for sees in raw.get("sees", []):
                sees.pop("match", None)                                    # 只给核对用，不进数据
            by_id[raw["id"]] = raw
        merged = list(by_id.values())
        CultureFile.model_validate({"city": city, "works": merged})           # 结构校验通过才写
        DATA.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"city": city, "works": merged}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        try:
            load_culture.cache_clear()
            load_culture(city)
        except CultureError as exc:
            print(f"✗ 写入后完整校验失败：{exc}")
            return 1
        print(f"已写入 {path.relative_to(ROOT)}（{len(merged)} 部作品）")
    if failed:
        print("有问题，没有写文件。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
