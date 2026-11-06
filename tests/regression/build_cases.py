"""生成回归用例（固定测试集）。

每个用例 = 一份 request + 一份 itinerary + 期望的质检 finding 代码集合。
用例从 golden 样例出发做定点改动，跑一次 build 冻结成 cases/*.json，
`test_regression.py` 只读这些文件、断言 validator 的输出没漂。

改了 validator 规则后，若确认新行为正确：
    python -m tests.regression.build_cases   # 重新冻结
再 review diff。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from app.schemas import Itinerary, TravelRequest
from app.validator import validate_itinerary

HERE = Path(__file__).resolve().parent
CASES_DIR = HERE / "cases"
ROOT = HERE.parents[1]
GOLDEN = json.loads((ROOT / "data" / "fixtures" / "stub-itinerary.json").read_text(encoding="utf-8"))
PERSONA1 = json.loads(
    (ROOT / "data" / "personas" / "persona-01-museum-foodie.json").read_text(encoding="utf-8")
)
PERSONA1.pop("_meta", None)


def _mut(fn) -> dict:
    d = copy.deepcopy(GOLDEN)
    fn(d)
    return d


# (name, description, request_dict, itinerary_dict) -------------------------- #
def _cases() -> list[tuple[str, str, dict, dict]]:
    out: list[tuple[str, str, dict, dict]] = []

    out.append(("clean-golden", "人工校对过的 4 天巴黎行程，应零 finding",
                PERSONA1, copy.deepcopy(GOLDEN)))

    # 把卢浮宫排到周二(10-06)——卢浮宫周二闭馆
    out.append(("closed-day-louvre-tuesday", "卢浮宫排在周二（闭馆）应报 closed_day",
                PERSONA1, _mut(lambda d: d["days"][3]["items"].insert(
                    0, {"time": "08:30", "title": "卢浮宫", "type": "museum",
                        "location": "1 区", "duration_minutes": 120, "transport": "地铁 1 号线",
                        "booking_required": True, "notes": None}))))

    out.append(("must-go-missing", "删掉塞纳河游船，必去项缺失",
                PERSONA1, _mut(lambda d: (d["days"][0]["items"].pop(2),
                                          d.__setitem__("booking_checklist",
                                                        [b for b in d["booking_checklist"]
                                                         if "游船" not in b["title"]])))))

    # 把卢浮宫也塞进周日(10-04)下午——和 golden 里周日上午的卢浮宫重复
    out.append(("repeated-attraction", "同一个博物馆出现在两天 → repeated_attraction",
                PERSONA1, _mut(lambda d: d["days"][2]["items"].insert(
                    0, {"time": "08:30", "title": "卢浮宫（二刷雕塑馆）", "type": "museum",
                        "location": "1 区", "duration_minutes": 30, "transport": "地铁 1 号线",
                        "booking_required": False, "notes": None}))))

    out.append(("avoid-hit-shopping", "出现购物项，用户 avoid shopping",
                PERSONA1, _mut(lambda d: d["days"][1]["items"].__setitem__(
                    3, {"time": "18:30", "title": "老佛爷百货购物", "type": "activity",
                        "location": "9 区", "duration_minutes": 90, "transport": "步行",
                        "booking_required": False, "notes": None}))))

    out.append(("booking-not-listed", "橘园标了需预约但清单里没有",
                PERSONA1, _mut(lambda d: d["days"][2]["items"][2].__setitem__(
                    "booking_required", True))))

    out.append(("day-overpacked-relaxed", "relaxed 节奏但某天塞了 8 小时活动",
                {**PERSONA1, "travel_style": "relaxed"},
                _mut(lambda d: [it.__setitem__("duration_minutes", 200)
                                for it in d["days"][1]["items"]])))

    def early_activity(d: dict) -> None:
        d["days"][0]["items"][1]["time"] = "07:30"   # persona1 抵达 10:30，观光项排在 7:30
        d["days"][0]["items"].sort(key=lambda i: i["time"])   # 保持升序，只留 before_arrival
    out.append(("before-arrival", "抵达日的观光活动早于抵达时间+缓冲",
                PERSONA1, _mut(early_activity)))

    out.append(("wrong-day-count", "只有 3 天，需求是 4 天",
                PERSONA1, _mut(lambda d: d.__setitem__("days", d["days"][:3]))))

    return out


def main() -> None:
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    for name, desc, req_raw, itin_raw in _cases():
        request = TravelRequest.model_validate({k: v for k, v in req_raw.items()})
        itinerary = Itinerary.model_validate(itin_raw)
        report = validate_itinerary(itinerary, request)
        case = {
            "name": name,
            "description": desc,
            "request": request.model_dump(mode="json"),
            "itinerary": itinerary.model_dump(mode="json"),
            "expect": {
                "ok": report.ok,
                "error_codes": sorted({f.code for f in report.errors}),
                "warning_codes": sorted({f.code for f in report.warnings}),
            },
        }
        (CASES_DIR / f"{name}.json").write_text(
            json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"{name:28} ok={report.ok} err={case['expect']['error_codes']} "
              f"warn={case['expect']['warning_codes']}")


if __name__ == "__main__":
    main()
