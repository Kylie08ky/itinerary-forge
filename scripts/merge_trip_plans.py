"""把旧的「旅行」（每座城市一份行程 + 一个总览页）并成一份多城市行程（规格 11 第 ③ 批，一次性迁移）。

    .venv\\Scripts\\python scripts/merge_trip_plans.py                    # 列出每趟旅行能不能并
    .venv\\Scripts\\python scripts/merge_trip_plans.py trip-20261031-xxxxxx   # 试并，只打印，不写
    .venv\\Scripts\\python scripts/merge_trip_plans.py trip-20261031-xxxxxx --apply   # 真的并，新建一份行程

只新增，不删：原来的每座城市的那几份行程原样留着，要不要删由你决定。合并用的是它们**现在的最新版本**
（你改过的内容、住宿、换城市的交通都带过去）；备注、文件附件、预订记录这些挂在原行程上的东西不搬。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import multicity  # noqa: E402
from app.trips import TripError  # noqa: E402
from app.versions import PlanStore  # noqa: E402

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "data" / "plans"


def trips(store: PlanStore) -> list[dict]:
    """库里旧的旅行表（新代码已经不再建这些表；没有就当没有）。"""
    with store.database.connect() as con:
        have = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"trips", "trip_legs"} <= have:
            return []
        out = []
        for t in con.execute("SELECT id, title FROM trips ORDER BY created_at"):
            legs = [dict(r) for r in con.execute(
                "SELECT l.position, l.destination, COALESCE(l.plan_id, k.plan_id) AS plan_id FROM trip_legs l "
                "LEFT JOIN tasks k ON k.id = l.task_id WHERE l.trip_id = ? ORDER BY l.position", (t["id"],))]
            estimated = {r["position"] for r in con.execute(
                "SELECT position FROM trip_transfers WHERE trip_id = ? AND estimated = 1", (t["id"],))}
            out.append({"id": t["id"], "title": t["title"], "legs": legs, "estimated": estimated})
        return out


def merge(store: PlanStore, trip: dict, *, apply: bool) -> str | None:
    missing = [leg["destination"] for leg in trip["legs"] if not leg["plan_id"]]
    if missing:
        raise TripError(f"{'、'.join(missing)}还没有生成好的行程，没法并")
    parts = [(store.request(leg["plan_id"]), store.load(leg["plan_id"])) for leg in trip["legs"]]
    for i in trip["estimated"]:             # 旧行程的需求里没记「交通是估算的」，这个标记在旅行表里：补回去
        a, b = parts[i][0], parts[i + 1][0]
        parts[i] = (a.model_copy(update={"transport": a.transport.model_copy(
            update={"inbound": a.transport.inbound.model_copy(update={"estimated": True})})}), parts[i][1])
        parts[i + 1] = (b.model_copy(update={"transport": b.transport.model_copy(
            update={"outbound": b.transport.outbound.model_copy(update={"estimated": True})})}), parts[i + 1][1])
    parent, itinerary = multicity.combine(parts)
    print(f"  合成：{multicity.title_of(parent)}，{len(itinerary.days)} 天，"
          f"{parent.start_date} → {parent.end_date}")
    if not apply:
        return None
    ids = "、".join(leg["plan_id"] for leg in trip["legs"])
    return store.create(parent, itinerary, note=f"由旧的旅行 {trip['id']} 合并（{ids}）")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trip_id", nargs="?", help="旅行号；不给就只列出")
    ap.add_argument("--apply", action="store_true", help="真的新建一份合并后的行程（默认只试并）")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="行程库目录（默认 data/plans）")
    args = ap.parse_args()
    store = PlanStore(args.root)
    found = trips(store)
    if not args.trip_id:
        for t in found:
            ready = all(leg["plan_id"] for leg in t["legs"])
            names = " → ".join(f"{leg['destination']}（{leg['plan_id'] or '没生成好'}）" for leg in t["legs"])
            print(f"{t['id']}  {'能并' if ready else '不能并'}  {names}")
        if not found:
            print("没有旧的旅行")
        return
    trip = next((t for t in found if t["id"] == args.trip_id), None)
    if trip is None:
        sys.exit(f"没有这趟旅行：{args.trip_id}")
    print(trip["title"])
    try:
        plan_id = merge(store, trip, apply=args.apply)
    except TripError as exc:
        sys.exit(f"并不了：{exc}")
    print(f"  新行程：{plan_id}" if plan_id else "  （试并，没有写入；加 --apply 才会新建）")


if __name__ == "__main__":
    main()
