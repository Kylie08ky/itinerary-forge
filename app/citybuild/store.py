"""现建城市的存放（规格 10）：`data/generated/{cities,places,themes}/<id>.json`，不进仓库（自动产物）。

读的地方（`app/cities.py`、`app/library.py`）先看精校的 `data/` 里有没有，没有再看这里。写是原子的：先写临时文件再改名，
所以读的人不会读到写了一半的文件。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_DEFAULT = Path(__file__).resolve().parents[2] / "data" / "generated"


def generated_dir() -> Path:
    """现建数据的目录；测试里可以用环境变量 CITY_GENERATED_DIR 指到临时目录。"""
    override = os.environ.get("CITY_GENERATED_DIR")
    return Path(override) if override else _DEFAULT


def generated_path(kind: str, city_id: str) -> Path:
    return generated_dir() / kind / f"{city_id}.json"


def has_places(city_id: str) -> bool:
    return generated_path("places", city_id).exists() and generated_path("themes", city_id).exists()


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def save_city_config(city: dict) -> None:
    """只存城市配置（认出城市就能存，地点数据之后再建）。"""
    _write(generated_path("cities", city["id"]), city)


def save_dishes(city_id: str, data: dict) -> None:
    """当地名菜（规格 10 H22）：单独存，没有分类的城市就没有这个文件。"""
    _write(generated_path("dishes", city_id), {"city": city_id, **data})


def save_generated(city: dict, places: list[dict], themes: dict) -> None:
    """先存地点和主题，最后存城市配置（配置在才算这座城市「存在」，所以放最后）。"""
    _write(generated_path("places", city["id"]), {"city": city["id"], "_note": "现建（开放数据草稿）：规格 10。",
                                                  "places": places})
    _write(generated_path("themes", city["id"]), themes)
    save_city_config(city)
