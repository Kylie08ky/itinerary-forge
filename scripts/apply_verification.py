"""把 verify_library.py 的核对报告落到地点库里（只改核对相关字段，保持「一行一个地点」的格式）。

用法：python scripts/apply_verification.py [--city paris] [--report path] [--today 2026-10-08]

规则（全部是确定性的，不靠人看）：
- 官网：请求成功 → url_status=ok；403 且域名在 BOT_PROTECTED_OK 里 → bot_protected；其他失败 → 报告里列出，不改数据。
- 坐标：Nominatim 最近候选 ≤ 250m → coords_verified=True；更远的不动，留在报告里。
- 闭馆日：OSM 的 opening_hours 能解析且与我们一致 → hours_verified=True；不一致只报告，不改数据。
- 地址：Nominatim 返回的最近候选（≤250m），已有地址不覆盖；抓不到的在报告里列出。
- OVERRIDES 是人工复核后的修正（官网 404、坐标明显错、连不上的链接置空），每条写明依据。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]

# 403 但确认是官方域名（站点有 Cloudflare 等反爬，自动请求进不去）。
BOT_PROTECTED_OK = {
    "www.musee-orsay.fr", "www.toureiffel.paris", "www.notredamedeparis.fr", "www.printemps.com",
    "www.fondationlouisvuitton.fr", "www.samaritaine.com", "www.mnhn.fr",
    # 国家签证 / 入境官方页面（scripts/build_countries.py）
    "www.mofa.go.jp", "travel.state.gov", "www.mfa.gr", "france-visas.gouv.fr",
}

# 没有门牌的街区 / 步行路线只给大致起点街道；其余按地图服务反查核对过（与我们坐标相差 ≤400m，长街除外）。
# --------------------------------------------------------------------------- #
# 购票方式（规格 M06）：规则是「有线上购票渠道就推荐线上，除非游客想收集纸质票根」。
#   online_required   必须提前线上购票（现场基本买不到 / 只卖时段票）
#   online_or_walkin  线上购票，现场也可排队购买
#   其余              无需购票 / 没有购票渠道（不在表里的地点一律是 none）
# 2026-10-09 对下面带说明的地点逐个查过（资料多为第三方站点，个别官方说法互相矛盾，说明里已写明），以官网为准。
# 拿不准的按「必须线上」：误报「现场能买」的代价（到了进不去）比误报「必须线上」大。
# --------------------------------------------------------------------------- #
TICKETING_CHECKED_AT = "2026-10-09"

TICKETING_NOTES: dict[str, str] = {
    "louvre": "官网强烈建议订带时间的电子票；现场窗口只在人少时有少量余票，基本等于必须线上订。上午场最先售罄。",
    "orsay": "多个 2026 年资料称：2026-03-10 起至 2028 年夏天，所有人（含博物馆通票持有者）都必须预订入场时段；"
             "官方 FAQ 的说法不一致，出发前请在官网确认。",
    "palais-garnier": "参观须订入场时段；官方称只有线上订能保证入场，现场售票说法不一；"
                      "12 月 20 日至 1 月 4 日现场不售票。排练时观众厅可能关闭。",
    "disneyland-paris": "多数票绑定具体日期，进园还需要当日乐园预约；各资料对现场买票说法不一（有的称已不设售票窗口），"
                        "建议提前线上买好。",
    "moulin-rouge": "几乎每场都满，当天现场基本买不到；据订票网站，建议至少提前一个月线上订，周五周末更早。",
    "eiffel-tower": "官网提前约 60 天开售电梯票；周末和旺季登顶票最先售罄。"
                    "现场有售票处，线上售罄时可去碰运气，但要排长队。",
    "sainte-chapelle": "线上订的票有固定安检时间；没订也能排队入场，忙时要多等 30 分钟以上。"
                       "大件行李不让进，也没有寄存处。",
    "versailles": "现场可买票，但官方建议线上订；所有票都是时段票。旺季买票队伍可能长达 2 小时，另有安检队伍。",
    "rodin": "个人游客现场可买票，也有自助机；线上订可保证时间，10 人以上团体必须提前订。",
    "fondation-louis-vuitton": "线上和现场售票处都能买，但现场每天票数有限，热门日可能售罄。",
    "orangerie": "官方强烈建议线上订时段票；每月第一个周日免费日必须线上订。",
    "picasso": "现场售票处开放；线上订时段票可优先入场、免排队（优先入场只在票面时间后半小时内有效）。",
    "pantheon": "当天可买票，但售票处常排长队，线上订时段票更省时间；享免费身份的游客也要订时段。",
    "arc-de-triomphe": "现场可买票，但排队约 45 分钟到 1 小时以上，建议线上买。",
}

MUST_ONLINE: set[str] = {
    "louvre", "orsay", "palais-garnier", "disneyland-paris", "moulin-rouge",           # 已查
    # 下面几个未逐个查，按原有「需预约」保守处理
    "lido", "crazy-horse", "seine-cruise", "ysl-museum", "galerie-dior", "philharmonie",
}
ONLINE_OR_WALKIN: set[str] = {
    "eiffel-tower", "sainte-chapelle", "versailles", "rodin", "fondation-louis-vuitton",
    "orangerie", "picasso", "pantheon", "arc-de-triomphe",                             # 已查
    "cluny", "marmottan", "conciergerie", "palais-de-tokyo", "quai-branly", "invalides",
    "bourse-de-commerce", "guimet", "arts-decoratifs", "palais-galliera", "grevin", "aquarium-paris",
    "cite-des-sciences", "tour-montparnasse", "jardin-dacclimatation", "grande-galerie-evolution",
    "caveau-huchette", "pompidou",                                                     # 收费但未逐个查
}

ADDRESS_OVERRIDES: dict[str, str] = {
    "montmartre-sacre-coeur": "35 Rue du Chevalier de la Barre, 75018 Paris",
    "notre-dame": "6 Parvis Notre-Dame - Place Jean-Paul II, 75004 Paris",
    "seine-cruise": "Port de la Bourdonnais, 75007 Paris",
    "lafayette-rooftop": "40 Boulevard Haussmann, 75009 Paris",
    "galeries-lafayette-shopping": "40 Boulevard Haussmann, 75009 Paris",
    "disneyland-paris": "Boulevard de Parc, 77700 Coupvray",
    "palais-galliera": "10 Avenue Pierre-Ier-de-Serbie, 75116 Paris",
    "galerie-dior": "30 Avenue Montaigne, 75008 Paris",
    "chanel-cambon": "31 Rue Cambon, 75001 Paris",
    "le-bon-marche": "24 Rue de Sèvres, 75007 Paris",
    "vallee-village": "3 Cours de la Garonne, 77700 Serris",
    "puces-saint-ouen": "Rue des Rosiers, 93400 Saint-Ouen-sur-Seine",
    "caveau-huchette": "5 Rue de la Huchette, 75005 Paris",
    "jardin-des-plantes": "57 Rue Cuvier, 75005 Paris",
    "laduree-champs": "75 Avenue des Champs-Élysées, 75008 Paris",
    "pierre-herme-st-germain": "72 Rue Bonaparte, 75006 Paris",
    "concorde": "Place de la Concorde, 75008 Paris",
    "place-vendome": "Place Vendôme, 75001 Paris",
    "avenue-montaigne": "Avenue Montaigne, 75008 Paris",
    "faubourg-saint-honore": "Rue du Faubourg Saint-Honoré, 75008 Paris",
    "tuileries-garden": "Jardin des Tuileries, 75001 Paris",
    # 地图数据里的路名不是游客会用的门牌（如 Port du Louvre），换成官方地址
    "louvre": "Rue de Rivoli, 75001 Paris",
    "orangerie": "Jardin des Tuileries, 75001 Paris",
    "arc-de-triomphe": "Place Charles de Gaulle, 75008 Paris",
    "printemps": "64 Boulevard Haussmann, 75009 Paris",
    "palais-de-tokyo": "13 Avenue du Président Wilson, 75116 Paris",
    "invalides": "129 Rue de Grenelle, 75007 Paris",
    "versailles": "Place d'Armes, 78000 Versailles",
    "cite-des-sciences": "30 Avenue Corentin Cariou, 75019 Paris",
    "carnavalet": "23 Rue de Sévigné, 75003 Paris",
    "ysl-museum": "5 Avenue Marceau, 75116 Paris",
    "crazy-horse": "12 Avenue George V, 75008 Paris",
    "philharmonie": "221 Avenue Jean Jaurès, 75019 Paris",
    "palais-garnier": "8 Rue Scribe, 75009 Paris",
    "grande-galerie-evolution": "36 Rue Geoffroy-Saint-Hilaire, 75005 Paris",
    "palais-royal": "Place du Palais-Royal, 75001 Paris",
    "latin-quarter": "37 Rue de la Bûcherie, 75005 Paris",
    "conciergerie": "2 Boulevard du Palais, 75001 Paris",
    "luxembourg-garden": "15 Rue de Vaugirard, 75006 Paris",
    "cafe-de-flore": "172 Boulevard Saint-Germain, 75006 Paris",
    "marais-walk": "Rue Madame de Sévigné, 75004 Paris",
       "canal-saint-martin": "Quai de Valmy, 75010 Paris",
    "marais-boutiques": "Rue Payenne, 75003 Paris",
    "st-germain-boutiques": "Rue de l'Abbaye, 75006 Paris",
}

OVERRIDES: dict[str, dict] = {
    "versailles": {"official_url": "https://www.chateauversailles.fr/",
                   "_why": "原 /en 返回 404；根域名 200，标题「Château de Versailles | Site officiel」"},
    "angelina": {"official_url": "https://www.angelina-paris.fr/",
                 "_why": "原 /en/ 返回 404；根域名 200"},
    "lido": {"official_url": "https://theatredulido.com/en/", "lat": 48.8723, "lng": 2.3007,
             "verified_at": "2026-10-09",
             "_why": "用户提供官网 theatredulido.com；真实浏览器 200，标题「Théâtre du Lido : Mythical Cabaret…」。"
                     "坐标按地址 116 bis av. des Champs-Élysées 地理编码（原坐标偏 500m 以上）"},
    "citypharma": {"official_url": None, "address": "26 rue du Four, 75006 Paris",
                   "_why": "零售药妆店，官网只是线上商城，对游客没用（用户决定）；给门店地址，Nominatim 已核对该地址"},
    "marmottan": {"lat": 48.8594, "lng": 2.2673,
                  "_why": "按地址 2 rue Louis Boilly 地理编码，原坐标偏 425m"},
    "vallee-village": {"lat": 48.8534, "lng": 2.7835,
                       "_why": "按 La Vallée Village 地理编码，原坐标偏 1.9km"},
}


def dump(path: Path, data: dict) -> None:
    head = {k: v for k, v in data.items() if k != "places"}
    lines = ["{"]
    for k, v in head.items():
        lines.append(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)},")
    lines.append('  "places": [')
    lines.append(",\n".join("    " + json.dumps(p, ensure_ascii=False) for p in data["places"]))
    lines.append("  ]")
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="paris")
    ap.add_argument("--report", default=None)
    ap.add_argument("--today", required=True)
    args = ap.parse_args()

    path = ROOT / "data" / "places" / f"{args.city}.json"
    report_path = Path(args.report) if args.report else ROOT / "data" / "places" / f"{args.city}.verification.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    report = {r["id"]: r for r in json.loads(report_path.read_text(encoding="utf-8"))}
    todo: list[str] = []

    for p in data["places"]:
        pid = p["id"]
        for k, v in OVERRIDES.get(pid, {}).items():
            if not k.startswith("_"):
                p[k] = v
        row = report.get(pid, {})

        # 官网（网络故障 / 超时没有 HTTP 状态码，属于「没查成」，沿用上次的结果，不当作失败）
        prev_status = p.pop("url_status", None)
        if p.get("official_url"):
            u = row.get("url") or {}
            host = urlparse(p["official_url"]).hostname
            if pid in OVERRIDES and "official_url" in OVERRIDES[pid]:
                p["url_status"] = "ok"          # 人工复核时已用真实浏览器确认 200
            elif u.get("ok"):
                p["url_status"] = "ok"
            elif u.get("status") == 403 and host in BOT_PROTECTED_OK:
                p["url_status"] = "bot_protected"
            elif u.get("status") is None and prev_status:
                p["url_status"] = prev_status
                todo.append(f"{pid}: 官网这次没连上（{u.get('error')}），沿用上次的核对结果")
            else:
                todo.append(f"{pid}: 官网 {p['official_url']} 核对失败（{u.get('status') or u.get('error')}）")
        # 坐标
        cands = (row.get("geo") or {}).get("candidates") or []
        if pid in OVERRIDES and "lat" in OVERRIDES[pid]:
            p["coords_verified"] = True
        elif cands and min(c["dist_m"] for c in cands) <= 250:
            p["coords_verified"] = True
        else:
            todo.append(f"{pid}: 坐标未能自动确认")
        # 购票方式
        tier = "online_required" if pid in MUST_ONLINE else "online_or_walkin" if pid in ONLINE_OR_WALKIN else "none"
        p["ticketing"] = tier
        p["booking"]["required"] = tier == "online_required"       # 与「必须线上」保持一致（库里有校验）
        p.pop("ticket_note", None)
        p.pop("ticketing_checked_at", None)
        if pid in TICKETING_NOTES:
            p["ticket_note"] = TICKETING_NOTES[pid]
            p["ticketing_checked_at"] = TICKETING_CHECKED_AT
        # 地址：人工修正表优先；已有的不动；没有就取离我们坐标 ≤250m 的最近候选
        if pid in ADDRESS_OVERRIDES:
            p["address"] = ADDRESS_OVERRIDES[pid]
        if not p.get("address"):
            near = [c for c in cands if c["dist_m"] <= 250 and c.get("address")]
            if near:
                p["address"] = min(near, key=lambda c: c["dist_m"])["address"]
            else:
                todo.append(f"{pid}: 没抓到地址，需要人工补")
        # 闭馆日
        osm = [c["closed_days_parsed"] for c in cands if c.get("closed_days_parsed") is not None]
        if osm and any(sorted(o) == sorted(p["closed_weekdays"]) for o in osm):
            p["hours_verified"] = True
        elif osm:
            todo.append(f"{pid}: 闭馆日 OSM={osm} 我们={p['closed_weekdays']}，不一致待复核")
        if p.get("official_url") or p.get("coords_verified") or p.get("hours_verified"):
            p["verified_at"] = OVERRIDES.get(pid, {}).get("verified_at", args.today)
        if not p.get("official_url") and "url_status" in p:
            del p["url_status"]

    dump(path, data)
    print(f"已更新 {path}；仍需关注 {len(todo)} 条：", file=sys.stderr)
    for t in todo:
        print("  -", t, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
