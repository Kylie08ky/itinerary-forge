"""维基数据 / 维基百科的小工具（给 build_culture.py 和 build_culture_auto.py 共用）。

名气 = 这个条目有多少个语言版本的维基百科页面（sitelinks 里以 wiki 结尾的，不算维基共享资源、维基语录等）。
这是个可以核对的数字，不是我的印象；用来决定「一个地点的故事谁排前面」。
"""

from __future__ import annotations

import re
import time
from urllib.parse import unquote, urlparse

import httpx

UA = "ai-travel-planner-agent-routes/0.1 (https://github.com/Kylie08ky; personal project)"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
_NOT_LANGUAGES = {"commonswiki", "wikidatawiki", "specieswiki", "metawiki", "mediawikiwiki", "wikimaniawiki",
                  "foundationwiki", "incubatorwiki", "outreachwiki", "sourceswiki", "wikitechwiki"}
_LANG_WIKI = re.compile(r"^[a-z_]+wiki$")


def make_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=60)


def get_json(client: httpx.Client, url: str, params: dict, *, tries: int = 4) -> dict:
    """带重试的 GET：维基的公共接口偶尔 429 / 5xx。"""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            r = client.get(url, params=params)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求失败：{url}：{last}")


def popularity(entity: dict) -> int:
    """实体的语言版本数。"""
    return sum(1 for k in entity.get("sitelinks", {}) if _LANG_WIKI.match(k) and k not in _NOT_LANGUAGES)


def entities(client: httpx.Client, ids: list[str], *, props: str, languages: str = "en") -> dict[str, dict]:
    """批量取实体（每次 50 个）。"""
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        data = get_json(client, WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(ids[i:i + 50]),
                                               "props": props, "languages": languages, "format": "json"})
        out.update(data.get("entities", {}))
    return out


def wikipedia_title(url: str) -> tuple[str, str] | None:
    """https://en.wikipedia.org/wiki/Foo_(film) → ("en", "Foo (film)")；不是维基百科网址 → None。"""
    u = urlparse(url)
    if not u.netloc.endswith(".wikipedia.org") or not u.path.startswith("/wiki/"):
        return None
    return u.netloc.split(".")[0], unquote(u.path[len("/wiki/"):]).replace("_", " ")


def qid_for_url(client: httpx.Client, url: str) -> str | None:
    """维基百科页面对应的维基数据编号（跟随重定向）。"""
    parsed = wikipedia_title(url)
    if parsed is None:
        return None
    lang, title = parsed
    data = get_json(client, f"https://{lang}.wikipedia.org/w/api.php", {
        "action": "query", "prop": "pageprops", "ppprop": "wikibase_item", "redirects": 1, "titles": title,
        "format": "json", "formatversion": 2})
    pages = data["query"]["pages"]
    return pages[0].get("pageprops", {}).get("wikibase_item") if pages and not pages[0].get("missing") else None


def works_at(client: httpx.Client, place_qid: str) -> list[tuple[str, str]]:
    """维基数据里「拍摄地点（P915）/ 故事发生地（P840）」是这个地点的条目：[(作品编号, 属性)]。"""
    query = (f'SELECT ?work ?prop WHERE {{ {{ ?work wdt:P915 wd:{place_qid} . BIND("P915" AS ?prop) }} '
             f'UNION {{ ?work wdt:P840 wd:{place_qid} . BIND("P840" AS ?prop) }} }}')
    data = get_json(client, SPARQL, {"query": query, "format": "json"})
    return [(b["work"]["value"].rsplit("/", 1)[1], b["prop"]["value"]) for b in data["results"]["bindings"]]
