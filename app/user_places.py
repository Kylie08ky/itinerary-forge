"""用户从高德里搜来加进地点库的点（规格 M05 S7d）。

存在 `data/generated/user-places/{城市}.json`（不进仓库）；和现建城市的数据分开存，城市数据重建时不丢。
只存名字、地址、坐标、类别这几样；没有开放时间、闭馆日、价格（高德的经营信息一概不收），所以是「开放数据草稿」档，
行程里每一处都会写「营业时间没核对，出发前请自己查」。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from app.citybuild.store import _write, generated_dir

UNCHECKED_NOTE = "营业时间没核对，出发前请自己查"
DEFAULT_MINUTES = {"sight": 90, "museum": 90}      # 其余类别 60 分钟
_INDOOR = {"museum", "shopping", "food", "entertainment"}


def _path(city_id: str) -> Path:
    return generated_dir() / "user-places" / f"{city_id}.json"


def load_user_places(city_id: str) -> list[dict]:
    path = _path(city_id)
    if not path.exists():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("places", []))
    except (ValueError, OSError):
        return []


def place_dict(*, name: str, address: str, area: str, category: str, lat: float, lng: float,
               today: date) -> dict:
    """高德搜到的点 → 地点库里的一条（草稿档，没有任何要核对的事实）。

    编号由名字和坐标算出（同一个点只加一次），不存高德的地点编号（服务协议不许存储服务数据，见 M05 S7b）。
    """
    key = f"{name}|{lat:.4f}|{lng:.4f}"
    return {
        "id": "amap-" + hashlib.sha1(key.encode()).hexdigest()[:10],
        "name_zh": name, "name_en": name, "category": category,
        "setting": "indoor" if category in _INDOOR else "outdoor", "zone": "core",
        "area": area or "市区", "lat": round(lat, 6), "lng": round(lng, 6),
        "duration_min": DEFAULT_MINUTES.get(category, 60), "address": address or None,
        "hours_verified": False, "coords_verified": False, "data_status": "draft",
        "draft_source": {"amap": "高德", "built_at": today.isoformat()},
    }


def save_user_place(city_id: str, place: dict) -> None:
    places = [p for p in load_user_places(city_id) if p.get("id") != place["id"]]
    places.append(place)
    _write(_path(city_id), {"city": city_id, "_note": "高德搜来的点（规格 M05 S7d）。", "places": places})
