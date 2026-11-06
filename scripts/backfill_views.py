"""给已经建好的城市补「热度」和「维基导游推荐」标记（规格 10 H21、M02 S9）。

用法：
    python scripts/backfill_views.py                    # data/generated/ 里所有城市
    python scripts/backfill_views.py --only quanzhou
    python scripts/backfill_views.py --dry-run          # 只看会补多少个

- 热度：每个地点在中文维基百科（维基数据里记的条目标题）最近 12 个整月的浏览量；没有中文条目的不补
- 维基导游：城市的英文版页面里列了这个地点（按维基数据编号对，没有编号的按汉字名完全相同对）→ 打标记 + 链接
只加 `fame.views` / `fame.views_period` / `voyage`，不改别的事实、不新增地点（要补新景点就重建那座城市）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.citybuild.fetch import UA, WIKIDATA  # noqa: E402
from app.citybuild.voyage import chinese_name, fetch_views, fetch_voyage, window  # noqa: E402

DATA = ROOT / "data" / "generated"


def zh_titles(client: httpx.Client, qids: list[str], pause: float = 1.0) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(qids), 50):
        r = client.get(WIKIDATA, params={"action": "wbgetentities", "ids": "|".join(qids[i:i + 50]),
                                         "props": "sitelinks", "format": "json"}, timeout=60)
        r.raise_for_status()
        for q, ent in (r.json().get("entities") or {}).items():
            title = ((ent.get("sitelinks") or {}).get("zhwiki") or {}).get("title")
            if title:
                out[q] = title
        time.sleep(pause)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    only = {x for x in args.only.split(",") if x}
    today = date.today()
    period = "{}~{}".format(*[w[:4] + "-" + w[4:6] for w in window(today)])
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        for path in sorted((DATA / "places").glob("*.json")):
            city = path.stem
            if city.count(".") or (only and city not in only):
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
            cfg = json.loads((DATA / "cities" / f"{city}.json").read_text(encoding="utf-8"))
            places = doc["places"]
            by_q = {(p.get("draft_source") or {}).get("wikidata"): p for p in places
                    if (p.get("draft_source") or {}).get("wikidata")}
            titles = zh_titles(client, sorted(by_q))
            n_views = 0
            for q, title in titles.items():
                p = by_q[q]
                if not p.get("fame"):
                    continue
                views = fetch_views(client, title, today)
                if views is not None:
                    p["fame"]["views"], p["fame"]["views_period"] = views, period
                    n_views += 1
            n_voyage = 0
            page = fetch_voyage(client, cfg["name_en"])
            if page:
                by_qid = {i.wikidata: i for i in page["listings"] if i.wikidata}
                by_name = {chinese_name(i): i for i in page["listings"] if chinese_name(i)}
                for p in places:
                    item = by_qid.get((p.get("draft_source") or {}).get("wikidata", "")) or by_name.get(p["name_zh"])
                    if item:
                        p["voyage"] = {"kind": item.kind, "url": page["url"], "revid": page["revid"],
                                       "checked_at": today.isoformat()}
                        n_voyage += 1
            print(f"{city}: 浏览量 {n_views}/{len(places)} 个；维基导游推荐 {n_voyage} 个"
                  + ("" if page else "（维基导游没有这座城市的页面）"))
            if not args.dry_run:
                path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
