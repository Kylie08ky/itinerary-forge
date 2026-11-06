"""重建一座「现建」的城市（data/generated/），让种子补点 / 排除和精校生效（规格 10 / 12）。

产品里用户输入目的地时自动现建；已经建好的城市不会自动重建，补了种子或精校之后用这个命令重建一次。
联网（维基数据、OpenStreetMap、维基共享资源），每座城市几分钟。

用法：
    python scripts/rebuild_generated_city.py rome florence milan naples
    python scripts/rebuild_generated_city.py --overlays-only rome       # 只把精校重新叠上去，不联网，几秒钟
    python scripts/rebuild_generated_city.py pisa=Q13375:比萨           # 新建：id=维基数据编号:中文名
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.citybuild.build import build_and_store, reapply_overlays  # noqa: E402
from app.citybuild.fetch import make_client  # noqa: E402
from app.citybuild.resolve import Resolver  # noqa: E402
from app.citybuild.store import generated_dir  # noqa: E402


def _say(msg: str) -> None:
    if msg.startswith(("核对 1/", "整理", "图片 1/")):
        print("  ", msg, flush=True)


def main(args: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    overlays_only = "--overlays-only" in args
    ids = [a for a in args if not a.startswith("--")]
    if overlays_only:
        for cid in ids:
            print(f"{cid}：叠了 {reapply_overlays(cid)} 个地点的精校")
        return 0
    for arg in ids:
        cid, _, rest = arg.partition("=")
        path = generated_dir() / "cities" / f"{cid}.json"
        if rest:                                       # 新建：id=QID:中文名
            qid, _, name_zh = rest.partition(":")
        elif path.exists():
            old = json.loads(path.read_text(encoding="utf-8"))
            qid, name_zh = old["wikidata"], old["name_zh"]
        else:
            print(f"{cid}：不是现建的城市（{path} 不存在）；新建写成 id=Q编号:中文名")
            continue
        with make_client() as client:
            r = Resolver(client)
            cands = r.describe([qid])
            if not cands or cands[0].lat is None:
                print(f"{cid}：维基数据里没有坐标")
                continue
            cands[0].label = name_zh
            seed = r.seed_for(cands[0], name_zh)
            seed["id"], seed["supported"] = cid, True
            print(f"== 重建 {cid}（{name_zh}）", flush=True)
            build_and_store(seed, client, _say)
            print(f"   完成 {cid}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
