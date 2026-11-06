"""给已建好的城市里还没有图的景点补图（建城市时一座城市最多下载 30 张，名气靠后的没有图）。

规则和建城市时一样（app/citybuild/media.py）：图片只用 Wikimedia Commons 上授权在白名单里的实拍图，来自这个地点的
维基数据条目里的 P18「图像」；作者 / 授权 / 来源 / 核对日期缺一项就不收；不由模型挑图。没有 P18、授权不合格的地点
照旧没有图（不拿别处的图凑）。只补缺的，已有的图和简介不动；只在手动运行时联网。

巴黎（手工整理的地点库）也一样：地点的维基数据编号在 `name_local_check.wikidata` 里，图片写进 data/places/。

用法：
    python scripts/fill_missing_photos.py --dry-run [城市 ...]   # 只列出能补哪些
    python scripts/fill_missing_photos.py [城市 ...]             # 补上并写回（不写城市 = 所有现建的城市，不含巴黎）
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

from app.citybuild.media import THUMB_WIDTH, _download, _write, commons_image  # noqa: E402
from app.citybuild.store import generated_dir  # noqa: E402
from app.library import load_library  # noqa: E402
from app.media import DATA_DIR, PHOTOS_DIR, PlaceImage  # noqa: E402

UA = "ai-travel-planner-agent-photos/0.1 (https://github.com/Kylie08ky; personal project)"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"


def p18_file(client: httpx.Client, qid: str, pause: float) -> str | None:
    """维基数据条目里的 P18（Commons 文件名）；优先标成「首选」的，其次第一条。"""
    time.sleep(pause)
    try:
        r = client.get(WIKIDATA_API, timeout=30, params={"action": "wbgetclaims", "entity": qid, "property": "P18",
                                                         "format": "json"})
        r.raise_for_status()
        claims = r.json().get("claims", {}).get("P18", [])
    except (httpx.HTTPError, ValueError):
        return None
    claims = sorted(claims, key=lambda c: c.get("rank") != "preferred")
    for c in claims:
        value = (c.get("mainsnak", {}).get("datavalue") or {}).get("value")
        if isinstance(value, str):
            return value
    return None


class Target:
    """一座城市要补图的地方：地点（编号、名字、维基数据编号）、media 文件、图片目录。"""

    def __init__(self, city_id: str):
        root = generated_dir()
        gen = root / "places" / f"{city_id}.json"
        self.city_id = city_id
        if gen.exists():                                               # 现建的城市
            raw = json.loads(gen.read_text(encoding="utf-8"))
            places = raw["places"] if isinstance(raw, dict) else raw
            self.places = [(p["id"], p["name_zh"], (p.get("draft_source") or {}).get("wikidata")) for p in places]
            self.media_path, self.photos_dir, self.indent = root / "media" / f"{city_id}.json", root / "photos", 2
        elif city_id == "paris":                                       # 手工整理的巴黎
            lib = load_library("paris")
            self.places = [(p.id, p.name_zh, p.name_local_check.wikidata if p.name_local_check else None)
                           for p in lib.places.values()]
            self.media_path, self.photos_dir, self.indent = DATA_DIR / "media.json", PHOTOS_DIR, 1
        else:
            raise SystemExit(f"{city_id}：不是现建的城市，也不是巴黎")


def fill_city(client: httpx.Client, city_id: str, *, dry_run: bool, pause: float = 0.3) -> tuple[int, int]:
    """返回 (缺图的地点数, 补上的数量)。"""
    t = Target(city_id)
    doc = (json.loads(t.media_path.read_text(encoding="utf-8")) if t.media_path.exists()
           else {"city": city_id, "places": {}})
    todo = [(pid, name, qid) for pid, name, qid in t.places
            if not (doc["places"].get(pid) or {}).get("image") and qid]
    added = 0
    for pid, name, qid in todo:
        file = p18_file(client, qid, pause)
        img = commons_image(client, file, THUMB_WIDTH, pause=pause) if file else None
        if not img:
            print(f"  ✗ {name}：{'没有 P18 图像' if not file else '图片授权 / 作者不合格'}")
            continue
        data = None if dry_run else _download(client, img["thumb_url"])
        if not dry_run and not data:
            print(f"  ✗ {name}：下载失败")
            continue
        ext = "png" if img["mime"] == "image/png" else "jpg"
        try:
            pi = PlaceImage(file=f"{pid}.{ext}", title=img["title"], source_url=img["source_url"],
                            author=img["author"], license=img["license"], license_url=img["license_url"],
                            width=img["width"], height=img["height"], checked_at=date.today())
        except ValueError as exc:
            print(f"  ✗ {name}：{str(exc).splitlines()[0][:60]}")
            continue
        print(f"  ✓ {name}：{img['author'][:30]}，{img['license']}")
        added += 1
        if not dry_run:
            _write(t.photos_dir / pi.file, data)
            doc["places"].setdefault(pid, {})["image"] = json.loads(pi.model_dump_json())
    if added and not dry_run:
        t.media_path.write_text(json.dumps(doc, ensure_ascii=False, indent=t.indent) + "\n", encoding="utf-8")
    return len(todo), added


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cities", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    root = generated_dir()
    cities = args.cities or sorted(p.stem for p in (root / "places").glob("*.json"))
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as client:
        for cid in cities:
            print(cid)
            todo, added = fill_city(client, cid, dry_run=args.dry_run)
            print(f"  {cid}：缺图 {todo}，{'能补' if args.dry_run else '补上'} {added}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
