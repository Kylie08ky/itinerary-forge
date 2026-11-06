"""导出里的街道底图（规格 M08 S2）：OpenStreetMap 标准瓦片 + 路线叠加，整张图是一段内嵌的 SVG。

- 瓦片只在导出时按需下载，存在本机缓存目录里（`data/tile-cache/`，不进仓库），同一块不重复下；一张图最多十几块，
  用得很少，遵守 OSM 瓦片使用政策：带可识别的 User-Agent、最多两个并发、图上署名「© OpenStreetMap contributors」。
- 库里的坐标是 WGS-84，和 OSM 瓦片一致；**不用高德的地图和数据**（高德的内容不许存进导出文件）。
- 任何一块瓦片下不到（离线、被拦）就返回 None，调用方退回自绘的示意图——导出永远出得来。
"""

from __future__ import annotations

import base64
import math
import os
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

TILE = 256
MAX_ZOOM = 16
MIN_ZOOM = 4
_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_USER_AGENT = "ai-travel-planner-agent/0.3 (personal project; route maps in exported itineraries)"
ATTRIBUTION = "© OpenStreetMap contributors"


def cache_dir() -> Path:
    env = os.environ.get("TILE_CACHE_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "data" / "tile-cache"


def _download(z: int, x: int, y: int) -> bytes | None:
    try:
        r = httpx.get(_URL.format(z=z, x=x, y=y), headers={"User-Agent": _USER_AGENT}, timeout=8.0)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    return r.content if r.headers.get("content-type", "").startswith("image/") else None


def tile_png(z: int, x: int, y: int) -> bytes | None:
    path = cache_dir() / str(z) / str(x) / f"{y}.png"
    if path.is_file():
        return path.read_bytes()
    data = _download(z, x, y)
    if data:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return data


def world_px(lat: float, lng: float, z: int) -> tuple[float, float]:
    """WGS-84 → Web Mercator 世界像素坐标（缩放级别 z）。"""
    scale = TILE * 2**z
    lat = max(-85.0511, min(85.0511, lat))
    x = (lng + 180) / 360 * scale
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def pick_zoom(points: list[tuple[float, float]], width: int, height: int, pad: int) -> int | None:
    """能把所有点装进画布的最大缩放级别；装不下（点分得太开）返回 None。"""
    for z in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        xs, ys = zip(*(world_px(lat, lng, z) for lat, lng in points), strict=True)
        if max(xs) - min(xs) <= width - 2 * pad and max(ys) - min(ys) <= height - 2 * pad:
            return z
    return None


def render(days: Iterable[tuple[str, list[tuple[float, float]]]], *, width: int = 700, height: int = 430,
           pad: int = 34) -> str | None:
    """days = [(颜色, [(纬度, 经度), ...]), ...] → 带底图的 SVG；底图拿不到返回 None。"""
    days = [(c, pts) for c, pts in days if pts]
    points = [p for _c, pts in days for p in pts]
    if not points:
        return None
    z = pick_zoom(points, width, height, pad)
    if z is None:
        return None
    px = [world_px(lat, lng, z) for lat, lng in points]
    cx, cy = (min(p[0] for p in px) + max(p[0] for p in px)) / 2, (min(p[1] for p in px) + max(p[1] for p in px)) / 2
    left, top = cx - width / 2, cy - height / 2
    limit = 2**z
    cols = range(math.floor(left / TILE), math.floor((left + width) / TILE) + 1)
    rows = range(math.floor(top / TILE), math.floor((top + height) / TILE) + 1)
    wanted = [(x, y) for y in rows for x in cols if 0 <= y < limit]
    with ThreadPoolExecutor(max_workers=2) as pool:
        got = list(pool.map(lambda t: tile_png(z, t[0] % limit, t[1]), wanted))
    if any(g is None for g in got):
        return None
    out = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
           f'xmlns:xlink="http://www.w3.org/1999/xlink" role="img" aria-label="全程路线图">'
           f'<defs><clipPath id="m"><rect width="{width}" height="{height}" rx="10"/></clipPath></defs>'
           f'<g clip-path="url(#m)"><rect width="{width}" height="{height}" fill="#f6f5f2"/>']
    for (x, y), data in zip(wanted, got, strict=True):
        b64 = base64.b64encode(data or b"").decode()
        out.append(f'<image x="{x * TILE - left:.1f}" y="{y * TILE - top:.1f}" width="{TILE}" height="{TILE}" '
                   f'href="data:image/png;base64,{b64}"/>')
    out.append(f'<rect width="{width}" height="{height}" fill="#fff" opacity="0.18"/>')       # 稍微压淡，路线更清楚

    def proj(lat: float, lng: float) -> tuple[float, float]:
        wx, wy = world_px(lat, lng, z)
        return wx - left, wy - top

    for color, pts in days:
        if len(pts) >= 2:
            line = " ".join(f"{x:.1f},{y:.1f}" for x, y in (proj(*p) for p in pts))
            out.append(f'<polyline points="{line}" fill="none" stroke="#fff" stroke-width="6" '
                       f'stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>'
                       f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="3.2" '
                       f'stroke-linejoin="round" stroke-linecap="round"/>')
    for color, pts in days:
        for k, p in enumerate(pts, start=1):
            x, y = proj(*p)
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8.5" fill="{color}" stroke="#fff" stroke-width="1.5"/>'
                       f'<text x="{x:.1f}" y="{y + 3.6:.1f}" text-anchor="middle" font-size="10" '
                       f'font-weight="700" fill="#fff">{k}</text>')
    out.append(f'<rect x="{width - 168}" y="{height - 17}" width="168" height="17" fill="#fff" opacity="0.8"/>'
               f'<text x="{width - 5}" y="{height - 5}" text-anchor="end" font-size="9.5" fill="#333">'
               f'{ATTRIBUTION}</text></g></svg>')
    return "".join(out)
