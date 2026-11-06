"""周边一日游的出发地和第一批目的地（规格 08 3.4）。
取城际时间（fetch_city_pairs.py）和建城市数据（build_light_city.py）共用。

这里只写「是哪座城市」——id、当地语言名、维基数据编号、车站怎么搜；中文名、坐标都从维基数据读，不在这里手写。
目的地是我基于常识的建议名单，不是事实；每座城市进库前都要有时刻表查到的单程时间，查不到的不收。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Origin:
    id: str
    name_local: str            # 当地语言写法
    qid: str                   # 维基数据编号
    station_query: str         # 在 Transitous 里搜车站用
    station_hints: tuple[str, ...]
    country: str


@dataclass(frozen=True)
class Destination:
    id: str
    name_local: str            # 当地语言写法（德语 / 荷兰语 / 法语…），写进城市配置的 name_en 位置
    qid: str
    origin: str                # 从哪个出发地来
    region: str
    country: str
    language: str              # 当地语言（ISO 639-1）：景点当地名用它取
    timezone: str
    station_query: str
    station_hints: tuple[str, ...]
    centre_radius_m: int = 2000   # 取景点的范围：市中心周围多少米
    station_exact: str | None = None   # 时刻表里火车站的确切站名（搜出来的第一个常常是同名的巴士站，指定后只认它）


ORIGINS = {
    "stuttgart": Origin("stuttgart", "Stuttgart", "Q1022", "Stuttgart Hbf", ("hbf", "hauptbahnhof"), "DE"),
    "aachen": Origin("aachen", "Aachen", "Q1017", "Aachen Hbf", ("hbf", "hauptbahnhof"), "DE"),
}

_DE = dict(country="DE", language="de", timezone="Europe/Berlin")
_RAIL_DE = ("hbf", "hauptbahnhof", "bahnhof", " bf")

DESTINATIONS: tuple[Destination, ...] = (
    # ---- 从 Stuttgart 斯图加特 ----
    Destination("ludwigsburg", "Ludwigsburg", "Q622", "stuttgart", "suedwest-de", station_query="Ludwigsburg",
                station_hints=_RAIL_DE, station_exact="Ludwigsburg", **_DE),
    Destination("esslingen", "Esslingen am Neckar", "Q3786", "stuttgart", "suedwest-de",
                station_query="Esslingen (N)", station_hints=_RAIL_DE, station_exact="Esslingen (N)", **_DE),
    Destination("heidelberg", "Heidelberg", "Q2966", "stuttgart", "suedwest-de", station_query="Heidelberg Hbf",
                station_hints=_RAIL_DE, **_DE),
    Destination("ulm", "Ulm", "Q3012", "stuttgart", "suedwest-de", station_query="Ulm Hbf",
                station_hints=_RAIL_DE, **_DE),
    Destination("tuebingen", "Tübingen", "Q3806", "stuttgart", "suedwest-de", station_query="Tübingen Hbf",
                station_hints=_RAIL_DE, **_DE),
    Destination("baden-baden", "Baden-Baden", "Q4100", "stuttgart", "suedwest-de", station_query="Baden-Baden Bahnhof",
                station_hints=_RAIL_DE, **_DE),
    Destination("strasbourg", "Strasbourg", "Q6602", "stuttgart", "suedwest-de", country="FR", language="fr",
                timezone="Europe/Paris", station_query="Strasbourg",
                station_hints=("gare", "strasbourg-ville", "strasbourg")),
    # ---- 从 Aachen 亚琛 ----
    Destination("liege", "Liège", "Q3992", "aachen", "nl-be-grenzland", country="BE", language="fr",
                timezone="Europe/Brussels", station_query="Liège-Guillemins", station_hints=("guillemins",)),
    Destination("koeln", "Köln", "Q365", "aachen", "nl-be-grenzland", station_query="Köln Hbf", station_hints=_RAIL_DE,
                **_DE),
    Destination("valkenburg", "Valkenburg aan de Geul", "Q9771", "aachen", "nl-be-grenzland", country="NL",
                language="nl", timezone="Europe/Amsterdam", station_query="Valkenburg", station_hints=("valkenburg",)),
    Destination("maastricht", "Maastricht", "Q1309", "aachen", "nl-be-grenzland", country="NL", language="nl",
                timezone="Europe/Amsterdam", station_query="Maastricht", station_hints=("maastricht",)),
    Destination("monschau", "Monschau", "Q200783", "aachen", "nl-be-grenzland", station_query="Monschau",
                station_hints=("monschau",), **_DE),
    Destination("bruxelles", "Bruxelles", "Q239", "aachen", "nl-be-grenzland", country="BE", language="fr",
                timezone="Europe/Brussels", station_query="Bruxelles-Midi", station_hints=("midi",)),
    Destination("duesseldorf", "Düsseldorf", "Q1718", "aachen", "nl-be-grenzland", station_query="Düsseldorf Hbf",
                station_hints=_RAIL_DE, **_DE),
)


def by_id(city_id: str) -> Destination:
    return next(d for d in DESTINATIONS if d.id == city_id)
