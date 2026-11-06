"""为地点库取图片和简介（规格 M02 S7b）：Wikimedia Commons 的授权实拍图 + 中文维基百科摘要。

用法：
    python scripts/fetch_place_media.py                  # 补上还没有的地点
    python scripts/fetch_place_media.py --only louvre,orsay
    python scripts/fetch_place_media.py --refresh        # 已有的也重新取
    python scripts/fetch_place_media.py --dry-run        # 只看会匹配到什么，不下载、不写文件

怎么保证「选对了地点」：用英文名在英文维基百科搜索，候选条目的**坐标必须落在地点库坐标 MATCH_RADIUS_M 米内**才采用，
没有坐标的条目不采用——所以同名的别处、没有坐标的泛称都进不来。图片和简介**不由模型挑 / 写**。
图片授权必须在白名单里（见 app/media.py），作者 / 授权 / 来源缺一项就不收。
只发公开的只读请求，每次请求之间等 1 秒，User-Agent 里写明用途。下载的是宽 THUMB_WIDTH 的缩略图，存进仓库。
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import time
import unicodedata
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.media import (  # noqa: E402
    DATA_DIR,
    MAX_INTRO_CHARS,
    PHOTOS_DIR,
    PlaceImage,
    PlaceIntro,
    PlaceMedia,
    license_allowed,
)

# Wikimedia 的机器人政策要求 User-Agent 里有联系方式（网址或邮箱），只写名字会被 403
UA = "ai-travel-planner-agent-media/0.1 (https://github.com/Kylie08ky; personal project)"
EN_API = "https://en.wikipedia.org/w/api.php"
ZH_API = "https://zh.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
ZH_SUMMARY = "https://zh.wikipedia.org/api/rest_v1/page/summary/{title}"
MATCH_RADIUS_M = 1000
THUMB_WIDTH = 640
MIN_ORIGINAL_WIDTH = 800          # 原图太小的不要（缩略图会糊）
MIN_FILE_BYTES = 8_000
MAX_FILE_BYTES = 450_000          # 太大的缩略图（多是 PNG）改要更窄的版本，仓库别被图片撑大
THUMB_WIDTHS = (THUMB_WIDTH, 480, 360)
# 兜底搜索时，文件名里带这些词的多半不是「这个地点的照片」
_NOT_A_PHOTO = {"logo", "map", "plan", "plattegrond", "menu", "poster", "flag", "coat", "icon", "symbol", "diagram",
                "drawing", "sketch", "cartoon", "scan", "stamp", "banknote", "ticket", "billet", "carte", "affiche",
                "painting", "engraving", "gravure", "blason"}
PAUSE_S = 1.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_GENERIC = {"the", "and", "des", "les", "aux", "sur", "paris", "musee", "museum", "parc", "park", "jardin", "jardins",
            "garden", "gardens", "palais", "palace", "place", "pont", "rue", "tour", "avenue", "boulevard", "church",
            "eglise", "cathedrale", "station", "square", "passage", "galerie", "gallery",
            # 地名里到处都是的词：「Saint-Germain 的店」不该匹配到「Saint-Sulpice 的喷泉」
            "saint", "sainte", "ste", "notre", "dame", "grand", "grande", "petit", "petite", "haut", "haute",
            "nord", "sud", "ouest"}
# 搜索时去掉这类「路线 / 逛街」后缀：「Le Marais walk」要搜的是 Le Marais
_NOISE = re.compile(r"\b(walk|boutiques?|shopping|cruise|rooftop|tour)\b", re.IGNORECASE)


def core_name(name_en: str) -> str:
    """去掉后缀杂词后的名字，用来在 Commons 里搜照片。去完什么都不剩就用原名。"""
    core = re.sub(r"\s+", " ", _NOISE.sub("", name_en)).strip(" -–—,")
    return core or name_en


# 标题里带这些词，说的多半是「旁边的另一样东西」（地铁站、剧院、动物园……）：除非地点自己的名字里就有这个词，否则不采用
_BAD_KIND = {"station", "metro", "rer", "gare", "theatre", "theater", "cinema", "hotel", "restaurant", "school",
            "lycee",
             "university", "hospital", "menagerie", "zoo", "hall", "church", "parish"}


def all_tokens(text: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return {t for t in re.split(r"[^a-z0-9]+", plain) if len(t) >= 3}


def name_tokens(text: str) -> set[str]:
    """名字里有区分度的词（去重音、小写、去掉「博物馆 / 公园 / 桥」这类泛称），用来确认候选条目说的是这个地点。"""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    words = {t for t in re.split(r"[^a-z0-9]+", plain) if len(t) >= 3}
    # 名字里全是泛称（比如 Notre-Dame）时，不能把它们都去掉，否则什么都匹配不上
    distinctive = {t for t in words if t not in _GENERIC}
    return distinctive or {t for t in words if t not in {"the", "and", "des", "les", "paris"}}


def same_name(name_en: str, title: str) -> bool:
    """名字对得上：有区分度的词至少一个相同，且标题里没有「地铁站 / 剧院 / 动物园」这类说明是别的东西的词。"""
    mine = name_tokens(name_en)
    if not mine & name_tokens(title):
        # 地点名里全是泛称（Notre-Dame）：只能拿这些泛称本身去对，别的词对不上就是对不上
        if not (mine <= _GENERIC and mine & all_tokens(title)):
            return False
    return not (_BAD_KIND & (all_tokens(title) - all_tokens(name_en)))


def name_score(name_en: str, title: str) -> float:
    """名字相似度（有区分度的词的 Jaccard）：几个候选都在范围内时，取最像的，而不是搜索排名第一的。"""
    a, b = name_tokens(name_en), name_tokens(title)
    return len(a & b) / max(len(a | b), 1)


_TAG = re.compile(r"<[^>]+>")


def clean_html(text: str) -> str:
    """Commons 的作者字段是一小段 HTML：去标签、还原实体、压缩空白。"""
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", text or ""))).strip()


def trim_intro(extract: str, limit: int = MAX_INTRO_CHARS) -> str | None:
    """摘要的前两句，不超过 limit 字，在句号处截断。第一句就超长时先去掉括号里的注音 / 外文名再试；还不行就不要。"""
    def sentences(text: str) -> list[str]:
        return [s for s in re.findall(r"[^。！？]+[。！？]", text.strip())]

    for text in (extract, re.sub(r"（[^（）]*）|\([^()]*\)", "", extract)):
        ss = sentences(re.sub(r"\s+", " ", text))
        if not ss:
            continue
        two = "".join(ss[:2])
        if len(two) <= limit:
            return two
        if len(ss[0]) <= limit:
            return ss[0]
    return None


class MediaFetcher:
    def __init__(self, client: httpx.Client, *, pause: float = PAUSE_S,
                 exclude: dict[str, set[str]] | None = None) -> None:
        self.client = client
        self.pause = pause
        self.exclude = exclude or {}

    def _get(self, url: str, **params) -> httpx.Response:
        time.sleep(self.pause)
        r = self.client.get(url, params=params or None, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return r

    # -- 找到对的条目 ----------------------------------------------------- #
    def find_page(self, name_en: str, lat: float, lng: float) -> dict | None:
        """看搜索排名前几的候选条目：名字要和地点对得上、坐标要落在 MATCH_RADIUS_M 米内，两条都满足的里面取名字最像的。

        两道保护缺一不可：只看坐标会把旁边的地铁站当成景点；只看名字会把别处的同名条目拿来。
        返回 {title, pageimage, zh_title, distance_m}；没有就返回 None。
        """
        found = self._get(EN_API, action="query", list="search", srsearch=f'"{name_en}" Paris', srlimit=6,
                          format="json").json()
        titles = [h["title"] for h in found.get("query", {}).get("search", [])]
        if not titles:
            return None
        info = self._get(EN_API, action="query", titles="|".join(titles), prop="coordinates|pageimages|langlinks",
                         colimit="max", piprop="name", lllang="zh", lllimit="max", redirects=1, format="json").json()
        pages = {p["title"]: p for p in info.get("query", {}).get("pages", {}).values()}
        best: tuple[float, int, dict] | None = None
        for rank, title in enumerate(titles):
            p = pages.get(title)
            if not p or not p.get("coordinates") or not same_name(name_en, title):
                continue
            c = p["coordinates"][0]
            dist = haversine_m(lat, lng, c["lat"], c["lon"])
            if dist > MATCH_RADIUS_M:
                continue
            zh = (p.get("langlinks") or [{}])[0].get("*")
            hit = {"title": title, "pageimage": p.get("pageimage"), "zh_title": zh, "distance_m": round(dist)}
            key = (-name_score(name_en, title), rank)                    # 越像越好；一样像时搜索排名靠前的优先
            if best is None or key < (-best[0], best[1]):
                best = (name_score(name_en, title), rank, hit)
        return best[2] if best else None

    # -- 简介 ----------------------------------------------------------- #
    def intro(self, zh_title: str) -> dict | None:
        try:
            r = self._get_summary(zh_title)
        except httpx.HTTPError:
            return None
        body = r.json()
        text = trim_intro(body.get("extract") or "")
        url = (body.get("content_urls") or {}).get("desktop", {}).get("page")
        if not text or not url:
            return None
        return {"text": text, "source_url": url}

    def _get_summary(self, zh_title: str) -> httpx.Response:
        time.sleep(self.pause)
        r = self.client.get(ZH_SUMMARY.format(title=zh_title.replace(" ", "_")), timeout=30, follow_redirects=True,
                            headers={"Accept-Language": "zh-CN"})
        r.raise_for_status()
        return r

    # -- 图片 ----------------------------------------------------------- #
    def image_info(self, file_name: str, width: int = THUMB_WIDTH) -> dict | None:
        """Commons 上这张图的授权信息 + 缩略图地址；授权不在白名单 / 缺作者 / 太小 / 不是照片就返回 None。"""
        title = file_name if file_name.startswith("File:") else f"File:{file_name.replace('_', ' ')}"
        if not re.search(r"\.(jpe?g|png)$", title, re.IGNORECASE):
            return None                                           # svg（地图、标志）、gif 等不要
        data = self._get(COMMONS_API, action="query", titles=title, prop="imageinfo",
                         iiprop="url|size|mime|extmetadata", iiurlwidth=width, format="json").json()
        page = next(iter(data.get("query", {}).get("pages", {}).values()), {})
        ii = (page.get("imageinfo") or [None])[0]
        if not ii or ii.get("mime") not in ("image/jpeg", "image/png"):
            return None
        meta = ii.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        if not license_allowed(lic):
            return None
        author = clean_html((meta.get("Artist") or {}).get("value", ""))
        if not author:
            if re.match(r"(?i)^(public domain|pd|cc0)", lic):
                author = "未署名（公有领域）"
            else:
                return None
        if ii.get("width", 0) < MIN_ORIGINAL_WIDTH or not 0.5 <= ii["width"] / max(ii.get("height", 1), 1) <= 2.6:
            return None
        return {
            "title": page.get("title", title), "source_url": ii["descriptionurl"], "author": author[:160],
            "license": lic.strip(), "license_url": (meta.get("LicenseUrl") or {}).get("value") or None,
            "thumb_url": ii["thumburl"], "width": ii.get("thumbwidth", THUMB_WIDTH),
            "height": ii.get("thumbheight", 0), "mime": ii["mime"],
            # 文件名 + 分类 + 描述：兜底搜索时用来确认「这是巴黎的」
            "text": " ".join([page.get("title", title), clean_html((meta.get("Categories") or {}).get("value", "")),
                              clean_html((meta.get("ImageDescription") or {}).get("value", ""))]),
        }

    def download(self, url: str, dest: Path, *, max_bytes: int | None = None) -> bool:
        """下载；文件太小（多半是错误页）或超过 max_bytes 返回 False，不落盘。"""
        time.sleep(self.pause)
        r = self.client.get(url, timeout=60, follow_redirects=True)
        if r.status_code != 200 or len(r.content) < MIN_FILE_BYTES:
            return False
        if max_bytes is not None and len(r.content) > max_bytes:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return True

    # -- 没有条目主图时：在 Commons 里直接搜 --------------------------------- #
    def commons_candidates(self, name_en: str, lat: float, lng: float) -> list[str]:
        """Commons 上和这个地点名字对得上的照片文件名：全文搜索排名在前，附近 200 米内有坐标的照片排在后面。"""
        name_en = core_name(name_en)
        found = self._get(COMMONS_API, action="query", list="search", srsearch=f'"{name_en}" Paris',
                          srnamespace=6, srlimit=12, format="json").json()
        titles = [h["title"] for h in found.get("query", {}).get("search", [])]
        geo = self._get(COMMONS_API, action="query", list="geosearch", gscoord=f"{lat}|{lng}", gsradius=200,
                        gsnamespace=6, gslimit=30, format="json").json()
        titles += [h["title"] for h in geo.get("query", {}).get("geosearch", []) if h["title"] not in titles]
        out = []
        for t in titles:
            bare = t.removeprefix("File:")
            if not re.search(r"\.(jpe?g|png)$", bare, re.IGNORECASE) or not same_name(name_en, bare):
                continue
            if _NOT_A_PHOTO & all_tokens(bare) - all_tokens(name_en):
                continue
            out.append(t)
        return out[:6]

    def commons_fallback(self, name_en: str, lat: float, lng: float, skip: set[str] | None = None) -> str | None:
        """兜底：返回第一张通过授权 / 尺寸检查、并且分类或描述里写着 Paris 的文件名；没有返回 None。"""
        for title in self.commons_candidates(name_en, lat, lng):
            if skip and title in skip:
                continue
            info = self.image_info(title)
            if info and "paris" in info["text"].lower():
                return title
        return None

    def _image_for(self, place_id: str, file_name: str, today: date, dry_run: bool) -> PlaceImage | None:
        for width in THUMB_WIDTHS:                    # 640 太大就依次试 480、360
            info = self.image_info(file_name, width)
            if not info:
                return None
            ext = ".png" if info["mime"] == "image/png" else ".jpg"
            file = f"{place_id}{ext}"
            if dry_run or self.download(info["thumb_url"], PHOTOS_DIR / file, max_bytes=MAX_FILE_BYTES):
                return PlaceImage(file=file, title=info["title"], source_url=info["source_url"],
                                  author=info["author"], license=info["license"], license_url=info["license_url"],
                                  width=info["width"], height=max(info["height"], 100), checked_at=today)
        return None

    # -- 一个地点 ------------------------------------------------------- #
    def fetch_place(self, place_id: str, name_en: str, lat: float, lng: float, *, dry_run: bool = False,
                    today: date | None = None) -> tuple[PlaceMedia | None, str]:
        """返回 (媒体记录或 None, 一句话说明)。先走「维基百科条目 → 主图 + 简介」，没有图再到 Commons 里直接搜。"""
        today = today or date.today()
        page = self.find_page(name_en, lat, lng)
        intro = None
        image = None
        where = "没有坐标在附近的维基百科条目"
        if page is not None:
            where = f"{page['title']}（{page['distance_m']}m）"
            if page["zh_title"]:
                raw = self.intro(page["zh_title"])
                if raw:
                    intro = PlaceIntro(text=raw["text"], source_url=raw["source_url"], checked_at=today)
            skip = self.exclude.get(place_id, set())
            main_image = page["pageimage"] or ""
            main_title = main_image if main_image.startswith("File:") else f"File:{main_image.replace('_', ' ')}"
            if main_image and main_title not in skip:
                image = self._image_for(place_id, main_image, today, dry_run)
        via_search = False
        if image is None:
            found = self.commons_fallback(name_en, lat, lng, self.exclude.get(place_id))
            if found:
                image = self._image_for(place_id, found, today, dry_run)
                via_search = image is not None
        if image is None and intro is None:
            return None, f"{where}；没有可用的图片（授权 / 尺寸 / 搜不到）也没有中文简介"
        bits = [("图（Commons 搜索）" if via_search else "图") if image else "无图", "简介" if intro else "无简介"]
        return PlaceMedia(image=image, intro=intro), f"{where}：{' + '.join(bits)}"


def prune_orphans(current: dict, photos_dir: Path | None = None) -> list[str]:
    """删掉 photos/ 里 media.json 已经不引用的图片（比如换了更严的匹配规则之后不再匹配的地点）。返回删掉的文件名。"""
    photos_dir = photos_dir or PHOTOS_DIR
    keep = {v["image"]["file"] for v in current["places"].values() if v.get("image")}
    gone = []
    for f in sorted(photos_dir.glob("*")):
        if f.is_file() and f.suffix in (".jpg", ".png") and f.name not in keep:
            f.unlink()
            gone.append(f.name)
    return gone


def load_exclusions(path: Path | None = None) -> dict[str, set[str]]:
    """人工看过图后排除的文件：{地点编号: {Commons 文件名}}（data/places/media_exclude.json，每条写明原因）。"""
    path = path or DATA_DIR / "media_exclude.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {pid: {f if f.startswith("File:") else f"File:{f}" for f in v["files"]} for pid, v in raw.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", default="paris")
    ap.add_argument("--only", default="", help="只处理这些地点编号，逗号分隔")
    ap.add_argument("--refresh", action="store_true", help="已有记录的地点也重新取")
    ap.add_argument("--missing-images", action="store_true", help="只处理目前还没有图片的地点（含完全没有记录的）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    places = json.loads((DATA_DIR / f"{args.city}.json").read_text(encoding="utf-8"))["places"]
    out_path = DATA_DIR / "media.json"
    current = {"city": args.city, "places": {}}
    if out_path.exists():
        current = json.loads(out_path.read_text(encoding="utf-8"))
    only = {x.strip() for x in args.only.split(",") if x.strip()}

    got = skipped = 0
    with httpx.Client(headers={"User-Agent": UA}) as client:
        fetcher = MediaFetcher(client, exclude=load_exclusions())
        for p in places:
            pid = p["id"]
            if only and pid not in only:
                continue
            has = current["places"].get(pid)
            if args.missing_images:
                if has and has.get("image"):
                    skipped += 1
                    continue
            elif has and not args.refresh:
                skipped += 1
                continue
            try:
                media, note = fetcher.fetch_place(pid, p["name_en"], p["lat"], p["lng"], dry_run=args.dry_run)
            except httpx.HTTPError as exc:
                print(f"[错误] {pid}: {type(exc).__name__}: {exc}")
                continue
            print(f"[{'ok' if media else '--'}] {pid}: {note}", flush=True)
            if args.dry_run:
                continue
            if media:
                current["places"][pid] = json.loads(media.model_dump_json(exclude_none=True))
                got += 1
            elif current["places"].pop(pid, None) is not None:
                print(f"     （重新匹配不到了，旧记录已删除：{pid}）")
            out_path.write_text(json.dumps(current, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if not only and not args.dry_run:
        pruned = prune_orphans(current)
        if pruned:
            print(f"清理了 {len(pruned)} 张没有记录对应的旧图片：{', '.join(pruned)}")
    print(f"\n新增 {got} 个，已有 {skipped} 个跳过；media.json 共 {len(current['places'])} 个地点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
