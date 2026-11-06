# ruff: noqa: E501  （数据脚本：长句是文案，不折行）
"""预建 / 重建一座城市的「标准档」数据（规格 10）。产品里用户输入新目的地时会自动现建，这个命令是给开发者预建、
看审阅表、或者往城市里补精校之后重建用的。

用法：
    python scripts/build_city.py --city beijing                # 读 data/city-seeds/beijing.json，写城市 / 地点 / 主题 + 审阅表
    python scripts/build_city.py --city beijing --dry-run      # 只打印审阅表，不写文件

流程和说明见 app/citybuild/（core.py 纯函数、fetch.py 取数据）和 specs/10-city-builder.md。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from apply_verification import dump  # noqa: E402

from app.citybuild.core import build_data, make_city, review_table  # noqa: E402
from app.citybuild.fetch import build_raws, make_client  # noqa: E402

DATA = ROOT / "data"
REPORTS = ROOT / "docs" / "reports"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--today", default=date.today().isoformat())
    args = ap.parse_args()
    today = date.fromisoformat(args.today)
    seed = load_json(DATA / "city-seeds" / f"{args.city}.json")
    if not seed:
        raise SystemExit(f"没有种子文件：data/city-seeds/{args.city}.json")
    overlays = load_json(DATA / "city-overlays" / f"{args.city}.json").get("places", {})
    print(f"== {args.city}", flush=True)
    with make_client() as client:
        raws = build_raws(seed, client, say=lambda m: print("  " + m, flush=True))
    places, themes = build_data(args.city, seed["name_zh"], raws, today, overlays)
    city = make_city(args.city, seed["name_zh"], seed["name_en"], tuple(seed["center"]), seed["bbox"],
                     [p["id"] for p in places], today=today, seed=seed)
    table = review_table(seed["name_zh"], places, raws, today)
    print(table)
    if args.dry_run:
        return 0
    (DATA / "cities" / f"{args.city}.json").write_text(json.dumps(city, ensure_ascii=False, indent=2) + "\n",
                                                       encoding="utf-8")
    dump(DATA / "places" / f"{args.city}.json",
         {"city": args.city, "_note": "标准档（开放数据草稿）：规格 10。", "places": places})
    (DATA / "themes" / f"{args.city}.json").write_text(json.dumps(themes, ensure_ascii=False, indent=2) + "\n",
                                                       encoding="utf-8")
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"city-{args.city}-draft.md").write_text(table, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
