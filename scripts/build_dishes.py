"""给现建城市补「当地名菜」（规格 10 H22）：中文维基百科的饮食分类 → data/generated/dishes/<城市>.json。

用法：
    python scripts/build_dishes.py                    # data/generated/ 里所有城市
    python scripts/build_dishes.py --only quanzhou,xi-an
    python scripts/build_dishes.py --dry-run          # 只打印会收哪些，不写文件

一条命令核对：分类能打开、每个条目的简介写明是食物、来源页面能打开；通过的才写入。
分类找不到 / 没有合格条目的城市不写文件（页面就不显示这一节），不编、不用模型补。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.citybuild.dishes import FetchFailed, build_dishes  # noqa: E402
from app.citybuild.fetch import CityFetcher, make_client  # noqa: E402
from app.citybuild.store import generated_dir, save_dishes  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    only = {x for x in args.only.split(",") if x}
    today = date.today()
    with make_client() as client:
        f = CityFetcher(client, 0.5)
        for path in sorted((generated_dir() / "cities").glob("*.json")):
            city = path.stem
            if only and city not in only:
                continue
            cfg = json.loads(path.read_text(encoding="utf-8"))
            name_zh = cfg["name_zh"]
            try:
                result = build_dishes(f, name_zh, today, lang=cfg.get("local_language", "zh"),
                                      city_qid=cfg.get("wikidata"))
            except FetchFailed as exc:
                print(f"{city}（{name_zh}）：取数据失败，没写文件，稍后重试：{exc}")
                continue
            if result is None:
                print(f"{city}（{name_zh}）：没有名菜（中文维基百科没有可用的饮食分类）")
                continue
            names = "、".join(d["name_zh"] for d in result["dishes"])
            print(f"{city}（{name_zh}）：「{result['category']}」{len(result['dishes'])} 样：{names}")
            if not args.dry_run:
                save_dishes(city, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
