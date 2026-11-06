"""给已经建好的城市补「名气分」（规格 M02 S9）：维基数据里每个地点的语言版本数。

用法：
    python scripts/backfill_fame.py                  # data/generated/ 里所有城市
    python scripts/backfill_fame.py --only quanzhou,rome
    python scripts/backfill_fame.py --dry-run        # 只看会补多少个

只读维基数据（wbgetentities，每次最多 50 个条目，请求之间停一秒）；只加 `fame` 字段，不改别的事实。
没有维基数据编号的点（你从高德搜来加的）不补，不编。
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

PLACES = ROOT / "data" / "generated" / "places"


def sitelink_counts(client: httpx.Client, qids: list[str], pause: float = 1.0) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in range(0, len(qids), 50):
        r = client.get(WIKIDATA, params={"action": "wbgetentities", "ids": "|".join(qids[i:i + 50]),
                                         "props": "sitelinks", "format": "json"}, timeout=60)
        r.raise_for_status()
        for q, ent in (r.json().get("entities") or {}).items():
            if "sitelinks" in ent:
                out[q] = len(ent["sitelinks"])
        time.sleep(pause)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    only = {x for x in args.only.split(",") if x}
    today = date.today().isoformat()
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        for path in sorted(PLACES.glob("*.json")):
            city = path.stem
            if city.count(".") or (only and city not in only):
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
            todo = {p["draft_source"]["wikidata"]: p for p in doc["places"]
                    if (p.get("draft_source") or {}).get("wikidata") and not p.get("fame")}
            if not todo:
                print(f"{city}: 已经都有名气分")
                continue
            counts = sitelink_counts(client, sorted(todo))
            for q, p in todo.items():
                if q in counts:
                    p["fame"] = {"sitelinks": counts[q], "source": "wikidata", "checked_at": today}
            print(f"{city}: 补了 {sum(1 for q in todo if q in counts)}/{len(todo)} 个")
            if not args.dry_run:
                path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
