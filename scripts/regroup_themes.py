"""给已经建好的城市按新的主题分法重新分组（规格 10 H20 / 场景 M38）：不重建、不改地点的任何事实。

用法：
    python scripts/regroup_themes.py                  # data/generated/ 里所有城市
    python scripts/regroup_themes.py --only quanzhou,rome
    python scripts/regroup_themes.py --dry-run

只读维基数据（每个地点的「是什么」，每次 50 个、请求之间停一秒）；只改主题文件和草稿地点的 interests。
没有维基数据编号的点（高德搜来的）保持原来的归类。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.citybuild.common import CLASS_TAGS  # noqa: E402
from app.citybuild.core import THEMES, best_class, make_themes, theme_for  # noqa: E402
from app.citybuild.fetch import UA, WIKIDATA  # noqa: E402
from app.citybuild.store import _write  # noqa: E402

GEN = ROOT / "data" / "generated"


def classes_of(client: httpx.Client, qids: list[str], pause: float = 1.0) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for i in range(0, len(qids), 50):
        r = client.get(WIKIDATA, params={"action": "wbgetentities", "ids": "|".join(qids[i:i + 50]),
                                         "props": "claims", "format": "json"}, timeout=60)
        r.raise_for_status()
        for q, ent in (r.json().get("entities") or {}).items():
            vals = [(c.get("mainsnak", {}).get("datavalue") or {}).get("value", {}).get("id")
                    for c in (ent.get("claims") or {}).get("P31", [])]
            out[q] = [v for v in vals if v in CLASS_TAGS]
        time.sleep(pause)
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    only = {x for x in args.only.split(",") if x}
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        for path in sorted((GEN / "places").glob("*.json")):
            city = path.stem
            if city.count(".") or (only and city not in only):
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
            themes_path = GEN / "themes" / f"{city}.json"
            old = json.loads(themes_path.read_text(encoding="utf-8"))
            city_zh = json.loads((GEN / "cities" / f"{city}.json").read_text(encoding="utf-8"))["name_zh"]
            qids = sorted({p["draft_source"]["wikidata"] for p in doc["places"]
                           if (p.get("draft_source") or {}).get("wikidata")})
            classes = classes_of(client, qids)
            old_theme = {m["place_id"]: t["id"] for t in old["themes"] for m in t["members"]}
            seed_path = ROOT / "data" / "city-seeds" / f"{city}.json"                  # 种子里手写的主题优先
            seed = json.loads(seed_path.read_text(encoding="utf-8")) if seed_path.exists() else {}
            override = {i["qid"]: i["theme"] for i in seed.get("include", []) if i.get("theme")}
            entries = []
            for p in doc["places"]:
                q = (p.get("draft_source") or {}).get("wikidata")
                fame = (p.get("fame") or {}).get("sitelinks", 0)
                if q and p.get("data_status") == "draft":
                    theme = theme_for(p["category"], best_class(classes.get(q, [])) or "", override.get(q))
                else:
                    theme = old_theme.get(p["id"], "heritage")
                    theme = theme if theme in THEMES else "heritage"
                entries.append({"id": p["id"], "theme": theme, "zone": p["zone"], "sitelinks": fame})
                if p.get("data_status") == "draft":
                    p["interests"] = list(THEMES[theme][2])
            new = make_themes(city, city_zh, entries)
            sizes = {t["id"]: len(t["members"]) for t in new["themes"]}
            print(f"{city}: {sizes}")
            if not args.dry_run:
                _write(path, doc)
                _write(themes_path, new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
