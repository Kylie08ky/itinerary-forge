# ruff: noqa: E501  （策展数据脚本：长句是数据，不折行）
"""热门目的地国家的随行清单数据（插头 / 电压、官方签证页），写进 data/companion/countries.json（你 2026-10-31 的要求）。

用法：
    python scripts/build_countries.py            # 核对官方链接能不能打开，写 data/companion/countries.json
    python scripts/build_countries.py --dry-run

规则（和别的策展数据一样）：
- 插头类型 / 电压 / 频率来自维基百科「Mains electricity by country」那张表（2026-10-31 逐国读出，下面 PLUGS 是原文）；
  国内（中国大陆）的是 A、I 型插头，220 V / 50 Hz，同一张表。
- **不写任何国家的签证政策**（免签 / 落地签 / 要不要办，说变就变）：只给该国官方的签证 / 入境页面链接，**链接要能打开才收**，
  打不开的不写链接、页面上就写「以官方渠道为准」；不信模型、不信第三方办证网站。
- 申根国家共用同一条护照规则（离开申根区后还要有 3 个月以上有效期、签发不满 10 年），来源是法国签证官网那条已核对的事实。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from apply_verification import BOT_PROTECTED_OK  # noqa: E402
from verify_library import BROWSER_UA, check_url  # noqa: E402

CHECKED = "2026-10-31"
PLUG_SOURCE = "https://en.wikipedia.org/wiki/Mains_electricity_by_country"
# 维基百科表里的原文：(插头类型, 电压 V, 频率 Hz)；电压写「居民插座的单相电压」（表里的 230/400 是单相 / 三相）
PLUGS: dict[str, tuple[list[str], int, str]] = {
    "JP": (["A", "B"], 100, "50 / 60"), "KR": (["C", "F"], 220, "60"), "SG": (["C", "G", "M"], 230, "50"),
    "MY": (["C", "G", "M"], 230, "50"), "TH": (["A", "B", "C", "O"], 230, "50"), "VN": (["A", "B", "C", "F"], 220, "50"),
    "ID": (["C", "F"], 230, "50"), "US": (["A", "B"], 120, "60"), "GB": (["G", "D", "M"], 230, "50"),
    "DE": (["C", "F"], 230, "50"), "IT": (["C", "F", "L"], 230, "50"), "ES": (["C", "F"], 230, "50"),
    "CH": (["C", "J"], 230, "50"), "AT": (["C", "F"], 230, "50"), "NL": (["C", "F"], 230, "50"),
    "GR": (["C", "F"], 230, "50"), "PT": (["C", "E", "F"], 230, "50"), "CZ": (["C", "E"], 230, "50"),
    "FR": (["C", "E"], 230, "50"), "AU": (["I"], 230, "50"), "TR": (["C", "F"], 230, "50"), "AE": (["G"], 230, "50"),
    "HK": (["G", "D", "M"], 220, "50"),
}
NAMES = {"JP": "日本", "KR": "韩国", "SG": "新加坡", "MY": "马来西亚", "TH": "泰国", "VN": "越南", "ID": "印度尼西亚",
         "US": "美国", "GB": "英国", "DE": "德国", "IT": "意大利", "ES": "西班牙", "CH": "瑞士", "AT": "奥地利",
         "NL": "荷兰", "GR": "希腊", "PT": "葡萄牙", "CZ": "捷克", "FR": "法国", "AU": "澳大利亚", "TR": "土耳其",
         "AE": "阿联酋", "HK": "中国香港"}
SCHENGEN = {"DE", "IT", "ES", "CH", "AT", "NL", "GR", "PT", "CZ", "FR"}
EUROPE = SCHENGEN | {"GB"}
# 各国官方的签证 / 入境页面（候选；能打开的才收）
VISA_PAGES: dict[str, tuple[str, str]] = {
    "JP": ("https://www.mofa.go.jp/j_info/visit/visa/index.html", "日本外务省"),
    "KR": ("https://www.visa.go.kr/", "韩国签证门户网站（HiKorea）"),
    "SG": ("https://www.ica.gov.sg/enter-transit-depart/entering-singapore/visa_requirements", "新加坡移民与关卡局"),
    "MY": ("https://www.imi.gov.my/", "马来西亚移民局"),
    "TH": ("https://thaievisa.go.th/", "泰国电子签证官网"),
    "VN": ("https://evisa.gov.vn/", "越南电子签证官网"),
    "ID": ("https://evisa.imigrasi.go.id/", "印尼移民局电子签证"),
    "US": ("https://travel.state.gov/content/travel/en/us-visas.html", "美国国务院"),
    "GB": ("https://www.gov.uk/check-uk-visa", "英国政府网站"),
    "DE": ("https://digital.diplo.de/visa", "德国外交部签证门户"),
    "IT": ("https://vistoperitalia.esteri.it/", "意大利外交部签证网站"),
    "CH": ("https://www.sem.admin.ch/sem/en/home/themen/einreise.html", "瑞士联邦移民局"),
    "AT": ("https://www.bmeia.gv.at/en/travel-stay/", "奥地利外交部（出行与居留）"),
    "NL": ("https://www.netherlandsworldwide.nl/visa-the-netherlands", "荷兰政府签证网站"),
    "GR": ("https://www.mfa.gr/en/visas/", "希腊外交部"),
    "PT": ("https://vistos.mne.gov.pt/en/", "葡萄牙外交部签证网站"),
    "CZ": ("https://www.mzv.gov.cz/jnp/en/information_for_aliens/index.html", "捷克外交部"),
    "FR": ("https://france-visas.gouv.fr/", "法国签证官网（France-Visas）"),
    "AU": ("https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-finder", "澳大利亚内政部"),
    "TR": ("https://www.evisa.gov.tr/en/", "土耳其电子签证官网"),
    "AE": ("https://icp.gov.ae/en/", "阿联酋联邦身份、公民、海关与港口安全局"),
}


def build(client: httpx.Client) -> dict:
    countries: dict[str, dict] = {}
    for iso, (types, volt, hz) in PLUGS.items():
        entry: dict = {"name_zh": NAMES[iso], "plug_types": types, "voltage_v": volt, "frequency_hz": hz,
                       "schengen": iso in SCHENGEN, "europe": iso in EUROPE}
        if iso in VISA_PAGES:
            url, name = VISA_PAGES[iso]
            res = check_url(client, url)
            host = url.split("/")[2]
            ok = bool(res.get("ok"))
            blocked = res.get("status") == 403 and host in BOT_PROTECTED_OK       # 官方域名、但自动请求被拦（和地点官网同一规则）
            print(f"{iso} {'✓' if ok else ('~' if blocked else '✗')} {url} ({res.get('status')})", flush=True)
            if ok or blocked:
                entry["visa_url"], entry["visa_name"] = res.get("final_url") or url, name
                entry["visa_url_status"] = "ok" if ok else "bot_protected"
        countries[iso] = entry
    return {
        "_note": "热门目的地国家的随行清单数据（规格 10 / M10）。插头 / 电压 / 频率来自维基百科「Mains electricity by country」；"
                 "签证只给官方页面链接（能打开才收），不写签证政策。scripts/build_countries.py 生成。",
        "checked_at": CHECKED, "plug_source_url": PLUG_SOURCE,
        "china": {"plug_types": ["A", "I"], "voltage_v": 220, "frequency_hz": "50"},
        "countries": countries,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    assert date.fromisoformat(CHECKED)
    with httpx.Client(headers={"User-Agent": BROWSER_UA}, follow_redirects=True) as client:
        data = build(client)
    if args.dry_run:
        return 0
    path = ROOT / "data" / "companion" / "countries.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已写入", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
