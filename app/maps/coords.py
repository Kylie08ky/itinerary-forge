"""WGS-84 ↔ GCJ-02（高德用的坐标系）换算（规格 M05 S7）。

库里、行程、导出的坐标一律存 WGS-84，只在调用高德（画图、搜索、路线、导航）时转成 GCJ-02。
公式是公开的偏移算法，自己实现，不引入依赖；中国境外不偏移。
"""

from __future__ import annotations

import math

_A = 6378245.0                    # 克拉索夫斯基椭球长半轴
_EE = 0.00669342162296594323      # 偏心率平方


def in_china(lat: float, lng: float) -> bool:
    """大陆范围（高德有细节的地方）：矩形再挖掉朝鲜半岛 / 日本一侧。和前端 web/src/mapkit.ts 同一套。"""
    if not (73.66 < lng < 135.05 and 3.86 < lat < 53.55):
        return False
    return not (lng > 123 and lat < 41)


def _dlat(x: float, y: float) -> float:
    r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _dlng(x: float, y: float) -> float:
    r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def wgs84_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    if not in_china(lat, lng):
        return lat, lng
    dlat = _dlat(lng - 105.0, lat - 35.0)
    dlng = _dlng(lng - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    magic = 1 - _EE * math.sin(rad) ** 2
    sq = math.sqrt(magic)
    dlat = dlat * 180.0 / ((_A * (1 - _EE)) / (magic * sq) * math.pi)
    dlng = dlng * 180.0 / (_A / sq * math.cos(rad) * math.pi)
    return lat + dlat, lng + dlng


def gcj02_to_wgs84(lat: float, lng: float) -> tuple[float, float]:
    """反向换算：迭代逼近，误差远小于 1 米。"""
    if not in_china(lat, lng):
        return lat, lng
    glat, glng = lat, lng
    for _ in range(5):
        clat, clng = wgs84_to_gcj02(glat, glng)
        glat += lat - clat
        glng += lng - clng
    return glat, glng
