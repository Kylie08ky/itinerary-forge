"""高德 Key（规格 M05 S7）：只放 .env，不进仓库、不进日志、不返回给前端以外的任何地方。

- `AMAP_WEB_KEY`：后端调 Web 服务（搜索、路线、地理编码），**不下发给前端**。
- `AMAP_JS_KEY` + `AMAP_JS_SECRET`：前端 JS API 2.0；本机自用直接下发，将来对外部署换成代理转发。
"""

from __future__ import annotations

import os

from app.llm import _load_dotenv


def _get(name: str) -> str:
    _load_dotenv()
    return os.getenv(name, "").strip()


def web_key() -> str:
    return _get("AMAP_WEB_KEY")


def js_keys() -> tuple[str, str] | None:
    key, secret = _get("AMAP_JS_KEY"), _get("AMAP_JS_SECRET")
    return (key, secret) if key and secret else None
