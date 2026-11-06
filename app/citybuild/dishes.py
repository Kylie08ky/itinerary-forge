"""现建城市的「当地名菜」（规格 10 H22）：取这座城市在中文维基百科的饮食分类。

开放数据里没有「每座城市吃什么」的现成清单；维基数据只记到国家，所以这里读中文维基百科的分类页
（「泉州小吃」「重庆饮食」……），分类里的每个条目取简介一句、语言版本数、近 12 个月浏览量。
只收简介里写明是食物的条目；找不到分类的城市就没有名菜，不编、不用模型补。

纯函数在上半部分，网络请求在下半部分；请求前后停顿，User-Agent 写明用途。
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import date
from urllib.parse import quote

import httpx

from app.citybuild.common import clean_name
from app.citybuild.fetch import CityFetcher, Progress, to_simplified
from app.citybuild.media import two_sentences
from app.citybuild.voyage import fetch_views

ZH_API = "https://zh.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
LOCAL_API = "https://{lang}.wikipedia.org/w/api.php"
# 能读「当地语言维基」饮食分类的语言；规则按语言写，没写规则的语言不读、不猜
LOCAL_LANGUAGES = {"it": "意大利语", "fr": "法语"}
CATEGORY_HEAD = {"it": "Categoria:Cucina ", "fr": "Catégorie:Cuisine "}
ZH_PAGE = "https://zh.wikipedia.org/wiki/{title}"
MAX_DISHES = 12
CATEGORY_NAMES = ("{c}小吃", "{c}饮食", "{c}美食", "{c}菜", "{c}饮食文化")
SUBCATEGORY_OK = re.compile(r"小吃|名菜|菜肴|美食|点心|饮食")
SUBCATEGORY_BAD = re.compile(r"公司|企业|品牌|餐厅|餐館|餐馆|酒|茶园|饮料|零售|超市|商店|连锁")
_FOOD_CHARS = ("食菜面饼餅汤湯糕粥饭飯肉鱼魚鸭鴨虾蝦蟹茶糖豆腐粉饺餃馍羹冻凍卷捲糍丸粿鹅鵝鸡雞羊牛猪豬蚝蠔蛎醋酱醬"
               "饮飲烧燒煎炸炒蒸煮烤粽糊餐吃肠腸酪乳奶蛋腿")
# 单独的「点」会误中「景点」，「包」会误中「包括」
_FOOD = re.compile(f"[{_FOOD_CHARS}]|点心|點心|面包|麵包|包子")
_NOT_FOOD = re.compile(
    r"街区|街區|一条街道|一條街道|一条街|地铁.{0,3}线|公司|企业|企業|集团|集團|品牌|景区|景區|道路|路段|大学|大學|医院|醫院|商会|商會|协会|協會"
    r"|古罗马|古希腊|古希臘|古代|拜占庭|史前|中世纪")        # 古代饮食史不是「现在这里吃什么」


# 繁体转简体只转字不转译名：中文维基里的意大利菜多是台湾译名，换成大陆通用的
MAINLAND_TERMS = {"义大利": "意大利", "義大利": "意大利", "义式": "意式", "義式": "意式", "拿坡里": "那不勒斯"}


def mainland(text: str) -> str:
    for taiwan, mainland_term in MAINLAND_TERMS.items():
        text = text.replace(taiwan, mainland_term)
    return text


def is_dish_extract(extract: str) -> bool:
    """简介前两句写明是食物（有食物字眼，且不是街区 / 公司 / 品牌）才算。"""
    head = two_sentences(extract, 200)
    return bool(head) and bool(_FOOD.search(head)) and not _NOT_FOOD.search(head)


def name_variants(city_zh: str) -> list[str]:
    """城市配置里常写「泉州市」「东京都」，维基分类叫「泉州小吃」「北京市小吃」都有：两种写法都试。"""
    short = re.sub(r"[市都府县]$", "", city_zh) if len(city_zh) > 2 else city_zh
    return list(dict.fromkeys([short, city_zh]))


def category_titles(city_zh: str) -> list[str]:
    return [f"Category:{pattern.format(c=name)}" for pattern in CATEGORY_NAMES for name in name_variants(city_zh)]


def pick_subcategories(titles: list[str]) -> list[str]:
    """分类里的子分类：叫小吃 / 名菜 / 美食的才往下读；公司、酒、餐厅这类不读。"""
    return [t for t in titles if SUBCATEGORY_OK.search(t) and not SUBCATEGORY_BAD.search(t)]


def rank(dishes: list[dict]) -> list[dict]:
    """名气（语言版本数）从高到低，同样的按浏览量，再按名字稳定排；最多 MAX_DISHES 样。"""
    return sorted(dishes, key=lambda d: (-d["sitelinks"], -(d.get("views") or 0), d["name_zh"]))[:MAX_DISHES]


def italian_adjectives(demonym: str) -> list[str]:
    """意大利语居民称呼 → 「Cucina ___」（阴性形容词）里的那个词：romano → romana，milanesi → milanese。"""
    d = demonym.strip().lower()
    if d.endswith("esi"):
        return [d[:-1] + "e"]
    if d.endswith("i"):
        return [d[:-1] + "a"]
    if d.endswith("o"):
        return [d[:-1] + "a"]
    return [d] if d else []


def french_adjectives(demonym: str) -> list[str]:
    """法语居民称呼 → 「Cuisine ___」里的阴性形容词：lyonnais → lyonnaise，parisien → parisienne，breton → bretonne。"""
    d = demonym.strip().lower()
    if d.endswith(("iens", "ains", "ons", "ands", "ards")):                    # 复数 → 单数
        d = d[:-1]
    if not d:
        return []
    if d.endswith("e"):
        return [d]
    return [d + "ne"] if d.endswith(("ien", "on")) else [d + "e"]


_ADJECTIVES = {"it": italian_adjectives, "fr": french_adjectives}
_PLURAL = {"it": lambda d: d.endswith("i"), "fr": lambda d: d.endswith(("iens", "ains", "ons", "ands", "ards"))}


def local_category_names(lang: str, demonyms: list[str]) -> list[str]:
    """当地语言维基里「某地的饮食」分类名。单数的称呼在就不再用复数的猜（romano / romani 只试 romana）；
    没写规则的语言返回空，不猜。"""
    if lang not in _ADJECTIVES:
        return []
    clean = [d.strip().lower() for d in demonyms if d.strip()]
    singular = [d for d in clean if not _PLURAL[lang](d)]
    out: list[str] = []
    for d in singular or clean:
        for adj in _ADJECTIVES[lang](d):
            name = f"{CATEGORY_HEAD[lang]}{adj}"
            if name not in out:
                out.append(name)
    return out


def bare_category(category: str) -> str:
    """「Categoria:Cucina laziale」→「Cucina laziale」。"""
    return category.split(":", 1)[-1]


def category_note(zh_categories: str, lang: str, local_category: str, regional: bool) -> str:
    """名菜卡底下那行来源说明：中文维基的分类、当地语言维基的分类（大区的要写明）。"""
    parts = []
    if zh_categories:
        parts.append(f"中文维基百科「{zh_categories}」")
    if local_category:
        parts.append(f"{LOCAL_LANGUAGES[lang]}维基百科「{bare_category(local_category)}」")
    return "取自" + "和".join(parts) + "分类" + ("（所在大区的饮食）" if regional and local_category else "")


# --------------------------------------------------------------------------- #
# 网络请求
# --------------------------------------------------------------------------- #
class FetchFailed(RuntimeError):
    """维基百科没取到（断网、被限流）：和「这座城市没有饮食分类」不是一回事，不能当成没有名菜。"""


def _members(f: CityFetcher, category: str, kind: str, api: str = ZH_API) -> list[str]:
    """分类里的条目（或子分类）。分类不存在 = 空列表；请求失败重试两次，还失败就抛 FetchFailed。"""
    last = ""
    for attempt in range(3):
        try:
            r = f.c.get(api, timeout=60, headers={"Accept": "application/json"},
                        params={"action": "query", "list": "categorymembers", "cmtitle": category, "cmtype": kind,
                                "cmlimit": 100, "format": "json"})
            time.sleep(0.5)
            if r.status_code == 200:
                return [m["title"] for m in r.json().get("query", {}).get("categorymembers", [])]
            last = f"HTTP {r.status_code}"
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            last = type(exc).__name__
        time.sleep(getattr(f, "pause", 1.0) * (attempt + 1))
    raise FetchFailed(f"读中文维基百科分类「{category}」失败（{last}）")


def _wikibase_ids(f: CityFetcher, titles: list[str]) -> dict[str, str]:
    """条目标题 → 维基数据编号（重定向后的规范标题也按原标题回填）。"""
    out: dict[str, str] = {}
    for i in range(0, len(titles), 40):
        chunk = titles[i:i + 40]
        try:
            r = f.c.get(ZH_API, timeout=60, headers={"Accept": "application/json"},
                        params={"action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
                                "titles": "|".join(chunk), "redirects": 1, "format": "json"})
            time.sleep(0.5)
            body = r.json().get("query", {})
        except (httpx.HTTPError, ValueError):
            continue
        redirect = {x["to"]: x["from"] for x in body.get("redirects", [])}
        for page in (body.get("pages") or {}).values():
            qid = (page.get("pageprops") or {}).get("wikibase_item")
            if qid:
                out[redirect.get(page["title"], page["title"])] = qid
                out[page["title"]] = qid
    return out


def _local_members(f: CityFetcher, lang: str, category: str) -> list[str]:
    """当地语言维基（意大利语）某个分类里的条目标题。"""
    return _members(f, category, "page", LOCAL_API.format(lang=lang))


def _entity(f: CityFetcher, qid: str, lang: str) -> dict:
    try:
        r = f.c.get(WIKIDATA_API, timeout=60, headers={"Accept": "application/json"},
                    params={"action": "wbgetentities", "ids": qid, "props": "claims", "format": "json"})
        time.sleep(0.5)
        return (r.json().get("entities") or {}).get(qid) or {}
    except (httpx.HTTPError, ValueError):
        return {}


def _current_parents(ent: dict) -> list[str]:
    """P131（所在行政区）里还在用的：带「结束时间」的是历史上的（威尼斯的「威尼斯省」旧政权），不要。"""
    out = []
    for cl in (ent.get("claims") or {}).get("P131", []):
        if "P582" in (cl.get("qualifiers") or {}) or cl.get("rank") == "deprecated":
            continue
        v = (cl.get("mainsnak", {}).get("datavalue") or {}).get("value")
        if isinstance(v, dict) and v.get("id"):
            out.append(v["id"])
    return out


def _ancestry(f: CityFetcher, qid: str, lang: str, depth: int = 4) -> list[dict]:
    """城市 → 所在省 → 所在大区……每一级带当地语言的居民称呼（P1549）。国家那一级不收（意大利菜不是这座城市的菜）。"""
    chain: list[dict] = []
    cur = qid
    for _ in range(depth):
        ent = _entity(f, cur, lang)
        parents = _current_parents(ent)
        if not parents:
            break
        demonyms = []
        for cl in (ent.get("claims") or {}).get("P1549", []):
            v = (cl.get("mainsnak", {}).get("datavalue") or {}).get("value")
            if isinstance(v, dict) and v.get("language") == lang and v.get("text"):
                demonyms.append(v["text"])
        chain.append({"qid": cur, "demonyms": demonyms})
        cur = parents[0]
    return chain


def _zh_titles(f: CityFetcher, lang: str, titles: list[str]) -> list[str]:
    """当地语言维基的条目 → 它在中文维基的标题（维基数据的语言链接）。没有中文条目的不要：简介要中文原文，不翻译。"""
    qids: list[str] = []
    for i in range(0, len(titles), 40):
        try:
            r = f.c.get(LOCAL_API.format(lang=lang), timeout=60, headers={"Accept": "application/json"},
                        params={"action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
                                "titles": "|".join(titles[i:i + 40]), "redirects": 1, "format": "json"})
            time.sleep(0.5)
            pages = (r.json().get("query") or {}).get("pages") or {}
        except (httpx.HTTPError, ValueError):
            continue
        qids += [q for p in pages.values() if (q := (p.get("pageprops") or {}).get("wikibase_item"))]
    if not qids:
        return []
    info = f.wikidata(sorted(set(qids)), ["zh"])
    out = [((info.get(q) or {}).get("sitelinks") or {}).get("zhwiki", {}).get("title") for q in sorted(set(qids))]
    return [t for t in out if t]


def local_dishes(f: CityFetcher, lang: str, city_qid: str, say: Progress = lambda _m: None
                 ) -> tuple[str, bool, list[str]] | None:
    """当地语言维基里这座城市的饮食分类 → (分类名, 是不是所在大区的, 有中文条目的那些条目的中文标题)。
    先找城市自己的，没有（或里面没有一个有中文条目）再往上找省、大区；都没有 → None。"""
    for level, node in enumerate(_ancestry(f, city_qid, lang)):
        for cat in local_category_names(lang, node["demonyms"]):
            pages = _local_members(f, lang, cat)
            zh = _zh_titles(f, lang, pages) if pages else []
            if zh:
                say(f"读{LOCAL_LANGUAGES[lang]}维基百科「{bare_category(cat)}」，{len(pages)} 个条目，"
                    f"{len(zh)} 个有中文条目")
                return cat, level > 0, zh
    return None


def build_dishes(f: CityFetcher, city_zh: str, today: date, say: Progress = lambda _m: None, lang: str = "zh",
                 city_qid: str | None = None) -> dict | None:
    """取数据中途断连（维基数据、维基百科）一律算「取数据失败」，不能崩，也不能当成没有名菜。"""
    try:
        return _build_dishes(f, city_zh, today, say, lang, city_qid)
    except httpx.HTTPError as exc:
        raise FetchFailed(f"取数据中途失败（{type(exc).__name__}）") from exc


def _build_dishes(f: CityFetcher, city_zh: str, today: date, say: Progress, lang: str,
                  city_qid: str | None) -> dict | None:
    """这座城市的名菜：{"city_zh", "category", "built_at", "dishes": [...]}；没有分类或没有一样合格 → None。
    外国城市（lang 不是 zh）每样名菜另取维基数据里当地语言的标签 name_local，城市也取一个当地名 city_local：
    前端用它们在 Google 地图里搜当地餐厅。没有标签就不写，不翻译、不用模型补。"""
    used: list[str] = []
    titles: list[str] = []
    for cat in category_titles(city_zh):
        pages = _members(f, cat, "page")
        for sub in pick_subcategories(_members(f, cat, "subcat")):
            pages += _members(f, sub, "page")
        if pages:
            used.append(cat.removeprefix("Category:"))
            titles += pages
    zh_only = set(titles)
    local = local_dishes(f, lang, city_qid, say) if lang in LOCAL_LANGUAGES and city_qid else None
    if local:
        titles += local[2]
    titles = list(dict.fromkeys(titles))                     # 几个分类里的同一个条目只算一次
    if not titles:
        say("中文维基百科和当地语言维基百科都没有这座城市的饮食分类，不列名菜")
        return None
    category = "、".join(used)
    if used:
        say(f"读中文维基百科「{category}」，{len(titles)} 个条目")
    qids = _wikibase_ids(f, titles)
    langs = ["zh"] if lang == "zh" else ["zh", lang]
    ids = sorted(set(qids.values()) | ({city_qid} if city_qid and lang != "zh" else set()))
    info = f.wikidata(ids, langs) if ids else {}
    keep: list[dict] = []
    seen_qids: set[str] = set()
    for title in titles:
        extract = f.zh_extract(title)
        if len(extract) < 20 or not is_dish_extract(extract):
            continue
        qid = qids.get(title)
        if qid and qid in seen_qids:                           # 繁体 / 简体标题指向同一条目，只算一次
            continue
        if qid:
            seen_qids.add(qid)
        sitelinks = len((info.get(qid or "", {}) or {}).get("sitelinks", {}))
        views = fetch_views(f.c, title, today)
        label = ((info.get(qid or "", {}) or {}).get("labels") or {}).get(lang, "") if lang != "zh" else ""
        keep.append({"title": title, "extract": extract, "qid": qid, "sitelinks": sitelinks, "views": views,
                     "local": label})
    if not keep:
        say("分类里没有写明是食物的条目，不列名菜")
        return None
    names = to_simplified(f.c, [k["title"] for k in keep] + [_summary(k["extract"]) for k in keep])
    dishes = rank([_dish(k, names, today) for k in keep])
    if not any(d["zh_wiki"] in zh_only for d in dishes):      # 中文分类里一样菜也没留下（罗马饮食只有古罗马）：不写
        category = ""
    out = {"city_zh": city_zh, "category": category,
           "category_note": category_note(category, lang, local[0] if local else "", bool(local and local[1])),
           "built_at": today.isoformat(), "dishes": dishes}
    city_local = ((info.get(city_qid or "", {}) or {}).get("labels") or {}).get(lang, "") if lang != "zh" else ""
    if city_local:
        out["city_local"] = city_local
    return out


_PHONETIC = re.compile(r"[（(][^）)]*[㄀-ㄯㆠ-ㆿ][^）)]*[）)]")      # 注音符号的括号


def _summary(extract: str) -> str:
    """简介：前两句（一句太短，比如「壶仔饭，是中餐的一种。」，就带上第二句），不超过 130 字；去掉括号里的注音。"""
    return two_sentences(_PHONETIC.sub("", extract), 130)


def _dish(k: dict, names: dict[str, str], today: date) -> dict:
    """一个条目 → 名菜。名字和简介转成简体；来源链到中文维基那一页。"""
    title, summary = k["title"], _summary(k["extract"])
    name = clean_name(mainland(names.get(title, title)))
    url = ZH_PAGE.format(title=quote(title.replace(" ", "_")))
    dish = {"id": "dish-" + hashlib.md5(title.encode()).hexdigest()[:8], "name_zh": name, "zh_wiki": title,
            "summary": mainland(names.get(summary, summary)),
            "source": {"title": f"{name}（中文维基百科）", "url": url, "checked_at": today.isoformat()},
            "sitelinks": k["sitelinks"]}
    if k["views"] is not None:
        dish["views"] = k["views"]
    if k["qid"]:
        dish["wikidata"] = k["qid"]
    if k.get("local"):
        dish["name_local"] = k["local"]
    return dish
