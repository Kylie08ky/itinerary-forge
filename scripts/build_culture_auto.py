# ruff: noqa: E501
"""从维基数据批量生成一座城市的「地点 → 作品」（规格 09 十五，B）：不用一座城市一座城市手写。

用法：
    python scripts/build_culture_auto.py --city paris              # 生成 data/culture/paris.auto.json
    python scripts/build_culture_auto.py --city paris --dry-run    # 只看报告，不写文件
    python scripts/build_culture_auto.py --city paris --explain    # 同时列出每个地点在来源里被提到的那一句
    python scripts/build_culture_auto.py --all                     # 地点库里有维基数据编号的所有城市

做法（每一步都是可核对的事实，没有模型参与）：
1. 地点库里每个地点的维基数据编号（取名脚本已核对过）。
2. 维基数据里「拍摄地点（P915）/ 故事发生地（P840）」是这个地点的作品。
3. 只留电影 / 纪录片 / 书（电视剧、游戏等不收）；名气（维基百科语言版本数，门槛见 app/culture.py 的 MIN_POPULARITY）不够的不收；要有英文维基百科页面、
   导演 / 作者和年份；中文名只取维基数据里的简体中文标签。
4. **来源页面里必须真的提到这个地点**（和手工策展同一条规则：作者姓、年份、地点名都要在页面全文里出现）；
   对不上的地点（维基数据有但页面没写）丢掉，作品没有任何地点对得上就整部丢掉。
5. 说明用固定句式（「拍摄地点之一。」/「故事发生的地点之一。」），不写简介——不写没有出处的话（页面上导演和年份已经在标题下面）。

人工策展的作品（data/culture/<城市>.json）永远优先：同一部作品（维基数据编号相同）不重复收录。
结果放在单独的 <城市>.auto.json，可以随时重跑覆盖，不会碰手工策展的文件。

它核对不了的：维基数据的拍摄地点本身偶尔有误（页面提到了地点，但不一定是在那里拍的）；所以说明只写「之一」，
并且按名气排序，行程里每个地点只显示名气最大的两条。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.culture import KINDS, MIN_POPULARITY, CultureError, CultureFile, mentions  # noqa: E402
from app.library import load_library  # noqa: E402
from app.names import fold  # noqa: E402
from scripts.build_culture import context  # noqa: E402
from scripts.build_route import fetch_text  # noqa: E402
from scripts.wikidata_util import entities, make_client, popularity, works_at  # noqa: E402

DATA = ROOT / "data" / "culture"
PLACES = ROOT / "data" / "places"

FILM = {"Q11424", "Q24862", "Q24869", "Q202866", "Q226730", "Q2431196", "Q1257444"}   # 电影 / 短片 / 长片 / 动画电影 / 默片 / 电影作品 / 剧情片
DOC = {"Q93204"}                                                                     # 纪录片
BOOK = {"Q571", "Q7725634", "Q8261", "Q47461344", "Q49084", "Q1318295"}               # 书 / 文学作品 / 小说 / 书面作品 / 短篇小说
NOTE = {"P915": "拍摄地点之一。", "P840": "故事发生的地点之一。"}
BOTH = "拍摄地点和故事发生地之一。"
CJK = re.compile(r"[一-鿿]")


def kind_of(p31: list[str]) -> str | None:
    ids = set(p31)
    if ids & DOC:
        return "documentary"
    if ids & FILM:
        return "film"
    if ids & BOOK:
        return "book"
    return None


def claim_ids(ent: dict, prop: str) -> list[str]:
    return [c["mainsnak"]["datavalue"]["value"]["id"] for c in ent.get("claims", {}).get(prop, [])
            if "datavalue" in c["mainsnak"]]


def claim_year(ent: dict) -> int | None:
    years = []
    for prop in ("P577", "P571"):
        for c in ent.get("claims", {}).get(prop, []):
            val = c["mainsnak"].get("datavalue", {}).get("value", {}).get("time")
            if val:
                years.append(int(val[1:5]))
        if years:
            return min(years)
    return None


def slug(title: str, year: int, qid: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    ascii_ = re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")
    return f"{ascii_}-{year}" if ascii_ else qid.lower()


def place_qids(city: str) -> dict[str, str]:
    raw = json.loads((PLACES / f"{city}.json").read_text(encoding="utf-8"))
    places = raw["places"] if isinstance(raw, dict) else raw
    return {p["id"]: p["name_local_check"]["wikidata"] for p in places
            if isinstance(p, dict) and (p.get("name_local_check") or {}).get("wikidata")}


def build(city: str, *, min_pop: int, max_works: int, explain: bool) -> tuple[list[dict], dict[str, int]]:
    client = make_client()
    lib = load_library(city)
    qids = {pid: q for pid, q in place_qids(city).items() if pid in lib.places and not lib.places[pid].long_term_closed}
    stats = {"地点": len(qids)}
    found: dict[str, dict[str, set[str]]] = {}                 # 作品编号 → {地点 id → {属性}}
    for pid, q in qids.items():
        for work, prop in works_at(client, q):
            found.setdefault(work, {}).setdefault(pid, set()).add(prop)
        time.sleep(0.15)
    stats["维基数据里关联的作品"] = len(found)

    ents = entities(client, list(found), props="labels|claims|sitelinks", languages="en|zh-hans|zh-cn")
    cands: list[tuple[str, dict, str, int]] = []
    for q, ent in ents.items():
        kind = kind_of(claim_ids(ent, "P31"))
        if kind is None:
            stats["不是电影 / 纪录片 / 书（电视剧、游戏等）"] = stats.get("不是电影 / 纪录片 / 书（电视剧、游戏等）", 0) + 1
            continue
        pop = popularity(ent)
        if pop < min_pop:
            stats["名气不够"] = stats.get("名气不够", 0) + 1
            continue
        cands.append((q, ent, kind, pop))
    cands.sort(key=lambda c: -c[3])
    cands = cands[:max_works]
    creator_ids = list({(claim_ids(e, "P50") or claim_ids(e, "P57") or [""])[0] for _q, e, _k, _p in cands} - {""})
    creators = entities(client, creator_ids, props="labels", languages="en") if creator_ids else {}

    today = date.today().isoformat()
    works: list[dict] = []
    for q, ent, kind, pop in cands:
        en_title = ent.get("labels", {}).get("en", {}).get("value")
        sitelink = ent.get("sitelinks", {}).get("enwiki", {}).get("title")
        cid = (claim_ids(ent, "P50") or claim_ids(ent, "P57") or [""])[0]
        creator = creators.get(cid, {}).get("labels", {}).get("en", {}).get("value") if cid else None
        year = claim_year(ent)
        if not (en_title and sitelink and creator and year):
            stats["缺英文页面 / 导演或作者 / 年份"] = stats.get("缺英文页面 / 导演或作者 / 年份", 0) + 1
            continue
        url = "https://en.wikipedia.org/wiki/" + sitelink.replace(" ", "_")
        try:
            text = fetch_text(client, url)
        except Exception:  # noqa: BLE001
            stats["来源页面打不开"] = stats.get("来源页面打不开", 0) + 1
            continue
        if fold(creator.split()[-1]) not in fold(text) or str(year) not in text:
            stats["来源页面里找不到导演 / 作者的姓或年份"] = stats.get("来源页面里找不到导演 / 作者的姓或年份", 0) + 1
            continue
        sees = []
        for pid, props in found[q].items():
            p = lib.places[pid]
            if not mentions(p, text):
                stats["地点：页面没提到，丢掉"] = stats.get("地点：页面没提到，丢掉", 0) + 1
                continue
            note = BOTH if len(props) > 1 else NOTE[next(iter(props))]
            sees.append({"place_id": pid, "note": note})
            if explain:
                print(f"  {en_title}: {pid}: …{context(text, p)}…")
        if not sees:
            stats["作品：没有任何地点能在页面里对上"] = stats.get("作品：没有任何地点能在页面里对上", 0) + 1
            continue
        zh = next((ent["labels"][lang]["value"] for lang in ("zh-hans", "zh-cn") if lang in ent.get("labels", {})), "")
        work = {
            "id": slug(en_title, year, q), "kind": kind, "title": en_title,
            "creator": creator, "year": year,
            "sees": sorted(sees, key=lambda s: s["place_id"]),
            "source": {"title": f"{sitelink}（维基百科）", "url": url, "checked_at": today},
            "wikidata": q, "popularity": pop,
        }
        if zh and CJK.search(zh) and zh != en_title:
            work["title_zh"] = zh
        works.append(work)
    return works, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--min-popularity", type=int, default=MIN_POPULARITY, help=f"维基百科至少有多少个语言版本（默认 {MIN_POPULARITY}）")
    ap.add_argument("--max-works", type=int, default=60, help="一座城市最多收几部（按名气，默认 60）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--explain", action="store_true")
    args = ap.parse_args()
    cities = sorted(p.stem for p in PLACES.glob("*.json") if p.stem not in {"hero", "hero_picks", "media", "media_exclude"}
                    and not p.stem.endswith((".local_names.review", ".verification"))) if args.all else args.city
    if not cities:
        ap.error("--city 或 --all")
    for city in cities:
        works, stats = build(city, min_pop=args.min_popularity, max_works=args.max_works, explain=args.explain)
        print(f"== {city}：收 {len(works)} 部  " + "；".join(f"{k} {v}" for k, v in stats.items()))
        for w in works:
            print(f"   {w['popularity']:>3}  {KINDS[w['kind']]}  {w.get('title_zh', '')} {w['title']} ({w['year']}) — "
                  + "、".join(s["place_id"] for s in w["sees"]))
        if args.dry_run:
            continue
        path = DATA / f"{city}.auto.json"
        if not works:
            path.unlink(missing_ok=True)
            continue
        CultureFile.model_validate({"city": city, "works": works})
        DATA.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"city": city, "works": works}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        from app.culture import load_culture
        for city in cities:
            load_culture.cache_clear()
            load_culture(city)
    except CultureError as exc:
        print(f"✗ 写入后完整校验失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
