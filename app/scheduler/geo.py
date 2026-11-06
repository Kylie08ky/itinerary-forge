"""距离与通勤估算。

v0.1 没有真实路线服务（M05 的 MapProvider 在 v0.2 接入），所以通勤一律是「直线距离 × 绕行系数 ÷ 经验速度」的
估算，Stop.travel_estimated 恒为 True，页面据此显示「估算」。接口保持纯函数，之后换成真实路线时只换这里。
"""

from __future__ import annotations

import math

Point = tuple[float, float]          # (lat, lng)

DETOUR = 1.3                         # 直线距离到实际路程的绕行系数
WALK_KMH = 4.8
WALK_MAX_KM = 1.5                    # 超过这个距离不再建议步行
METRO_KMH = 22.0                     # 含换乘与等待的平均速度
METRO_BASE_MIN = 10                  # 进站、候车
RAIL_KMH = 55.0                      # 市郊铁路（RER / Transilien）
RAIL_BASE_MIN = 10
RAIL_FROM_KM = 12.0                  # 直线距离超过它按市郊铁路算
CAR_KMH = 28.0                       # 自驾：市区平均车速（红绿灯、拥堵）
CAR_BASE_MIN = 8                     # 自驾：取车 / 找停车位
CAR_FAR_KMH = 60.0                   # 自驾：直线超过 RAIL_FROM_KM 的远郊，能走快速路
LONG_DETOUR = 1.25                   # 长途自驾：路程 ≈ 直线距离 × 1.25
LONG_KMH = 75.0                      # 长途自驾平均时速（含服务区限速、收费站）
REST_MIN = 15                        # 每开约 2 小时休息一次


def haversine_km(a: Point, b: Point) -> float:
    (lat1, lng1), (lat2, lng2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def travel(a: Point, b: Point, *, car: bool = False) -> tuple[int, str]:
    """返回 (分钟, 方式)。同一地点返回 (0, "步行")。car = 选了自驾：步行以外都按开车算。"""
    straight = haversine_km(a, b)
    road = straight * DETOUR
    if road <= WALK_MAX_KM:
        return math.ceil(road / WALK_KMH * 60), "步行"
    if car:
        far = straight >= RAIL_FROM_KM
        return CAR_BASE_MIN + math.ceil(road / (CAR_FAR_KMH if far else CAR_KMH) * 60), "自驾"
    if straight >= RAIL_FROM_KM:
        return RAIL_BASE_MIN + math.ceil(road / RAIL_KMH * 60), "RER/火车"
    return METRO_BASE_MIN + math.ceil(road / METRO_KMH * 60), "地铁"


def drive_estimate(straight_km: float) -> tuple[int, int]:
    """出发地到目的地的直线距离 → (大致路程公里, 大致分钟)。只是估算，不查路线：路程取直线的 1.25 倍，
    平均时速 75 公里，每开约 2 小时休息 15 分钟。"""
    if straight_km <= 0:
        return 0, 0
    road = straight_km * LONG_DETOUR
    hours = road / LONG_KMH
    return round(road), round(hours * 60 + int(hours // 2) * REST_MIN)
