"""封面大图（规格 M02 S7c）：给少数标志性地点取一张 ≥ 1600 宽的横版实拍大图，用作首页背景和行程页封面。

两步，都只在手动运行时联网（只读请求，1 次 / 秒，带说明用途的 User-Agent）：

1. 找候选（人看一眼再定）：
       python scripts/fetch_hero_images.py --candidates [--places eiffel-tower,louvre] [--sheet 候选图.html]
   在 Commons 里搜这个地点的照片，只留通过检查的（JPEG、原图宽 ≥ 2400、横版 ≥ 1.35、授权在白名单里、有作者），
   并生成一页「候选图一览」让人用眼睛挑——程序分不出哪张好看、哪张拍的是别的东西。
2. 取图：把挑中的文件名写进 `data/places/hero_picks.json`
   （{"picks": {"地点id": {"title": "File:xxx.jpg", "why": "为什么选它"}}}），然后
       python scripts/fetch_hero_images.py
   脚本逐个核对授权 / 作者 / 尺寸，下载 1920 宽（太大就 1600）的缩略图到 `data/places/hero/`，
   写 `data/places/hero.json`。

硬规则（和地点缩略图一样）：授权必须在白名单（CC0 / 公有领域 / CC BY / CC BY-SA），作者、授权、来源链接缺一项不收；
不由模型挑图；页面上署名永远和图片一起显示。
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_place_media import COMMONS_API, UA, clean_html  # noqa: E402

from app.media import HERO_DIR, HERO_PATH, HeroImage, license_allowed  # noqa: E402

PICKS_PATH = ROOT / "data" / "places" / "hero_picks.json"
MIN_ORIGINAL_WIDTH = 2400
MIN_RATIO = 1.35
MAX_FILE_BYTES = 900_000
HERO_WIDTHS = (1920, 1600)
PAUSE_S = 1.0
# 搜得到的先看「特色图片 / 优质图像」，它们的构图和清晰度通常最好
QUERY_PREFIXES = ('incategory:"Featured pictures on Wikimedia Commons"', 'incategory:"Quality images"', "")


class HeroError(RuntimeError):
    """这张图不能用（带原因，打印给人看）。"""


class Fetcher:
    def __init__(self, client: httpx.Client, *, pause: float = PAUSE_S) -> None:
        self.client, self.pause = client, pause

    def _get(self, url: str, **params) -> httpx.Response:
        time.sleep(self.pause)
        r = self.client.get(url, params=params or None, timeout=60, follow_redirects=True)
        r.raise_for_status()
        return r

    def info(self, title: str, width: int = HERO_WIDTHS[0]) -> dict:
        """Commons 上这个文件的授权 + 尺寸 + 缩略图地址；不合格抛 HeroError（带原因）。"""
        title = title if title.startswith("File:") else f"File:{title}"
        data = self._get(COMMONS_API, action="query", titles=title, prop="imageinfo",
                         iiprop="url|size|mime|extmetadata", iiurlwidth=width, format="json").json()
        page = next(iter(data.get("query", {}).get("pages", {}).values()), {})
        ii = (page.get("imageinfo") or [None])[0]
        if not ii:
            raise HeroError(f"{title}：Commons 上找不到这个文件")
        if ii.get("mime") != "image/jpeg":
            raise HeroError(f"{title}：不是 JPEG（{ii.get('mime')}）")
        meta = ii.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "").strip()
        if not license_allowed(lic):
            raise HeroError(f"{title}：授权「{lic or '未标注'}」不在白名单里")
        author = clean_html((meta.get("Artist") or {}).get("value", ""))
        if not author:
            raise HeroError(f"{title}：没有作者信息")
        w, h = ii.get("width", 0), max(ii.get("height", 1), 1)
        if w < MIN_ORIGINAL_WIDTH:
            raise HeroError(f"{title}：原图宽 {w}，小于 {MIN_ORIGINAL_WIDTH}")
        if w / h < MIN_RATIO:
            raise HeroError(f"{title}：宽高比 {w / h:.2f}，不是横版（要 ≥ {MIN_RATIO}）")
        return {
            "title": page.get("title", title), "source_url": ii["descriptionurl"], "author": author[:160],
            "license": lic, "license_url": (meta.get("LicenseUrl") or {}).get("value") or None,
            "thumb_url": ii["thumburl"], "thumb_width": ii.get("thumbwidth", width),
            "thumb_height": ii.get("thumbheight", 0), "original": (w, h),
        }

    def candidates(self, query: str, limit: int = 8) -> list[dict]:
        """搜 Commons，返回通过检查的候选（带 480 宽缩略图地址，给「候选图一览」用）。"""
        out: list[dict] = []
        seen: set[str] = set()
        for prefix in QUERY_PREFIXES:
            found = self._get(COMMONS_API, action="query", list="search", srsearch=f"{prefix} {query}".strip(),
                              srnamespace=6, srlimit=12, format="json").json()
            for hit in found.get("query", {}).get("search", []):
                t = hit["title"]
                if t in seen or not t.lower().endswith((".jpg", ".jpeg")):
                    continue
                seen.add(t)
                try:
                    info = self.info(t, width=480)
                except HeroError:
                    continue
                out.append({**info, "featured": prefix.startswith("incategory:\"Featured")})
                if len(out) >= limit:
                    return out
        return out

    def download(self, url: str, dest: Path) -> int | None:
        """下载；太小（多半是错误页）返回 None，否则返回字节数。"""
        r = self._get(url)
        if len(r.content) < 20_000:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return len(r.content)

    def fetch_pick(self, place_id: str, title: str, *, today: date, hero_dir: Path = HERO_DIR) -> HeroImage:
        for width in HERO_WIDTHS:                             # 1920 太大就依次试 1600
            info = self.info(title, width)
            dest = hero_dir / f"{place_id}.jpg"
            size = self.download(info["thumb_url"], dest)
            if size is None:
                raise HeroError(f"{title}：下载到的文件太小，多半是错误页")
            if size <= MAX_FILE_BYTES:
                return HeroImage(file=dest.name, title=info["title"], source_url=info["source_url"],
                                 author=info["author"], license=info["license"], license_url=info["license_url"],
                                 width=info["thumb_width"], height=max(info["thumb_height"], 100), checked_at=today)
            dest.unlink(missing_ok=True)
        raise HeroError(f"{title}：{HERO_WIDTHS[-1]} 宽也超过 {MAX_FILE_BYTES // 1000}KB，换一张")


def load_picks(path: Path = PICKS_PATH) -> dict[str, dict]:
    return json.loads(path.read_text(encoding="utf-8")).get("picks", {}) if path.exists() else {}


def sheet_html(rows: dict[str, list[dict]]) -> str:
    """候选图一览：每个地点一排，缩略图 + 文件名 + 作者 + 授权，点图看大图。"""
    parts = ["<!doctype html><meta charset=utf-8><title>候选图一览</title>"
             "<style>body{font:14px system-ui;margin:20px;background:#f4f1ea}h2{margin:28px 0 8px}"
             ".row{display:flex;flex-wrap:wrap;gap:12px}.c{width:300px;background:#fff;padding:8px;border-radius:8px}"
             ".c img{width:100%;height:190px;object-fit:cover;border-radius:4px}.c small{display:block;color:#555;"
             "word-break:break-all;margin-top:4px}.f{color:#b88a38;font-weight:700}</style>"]
    for pid, cands in rows.items():
        parts.append(f"<h2>{html.escape(pid)}（{len(cands)} 张）</h2><div class=row>")
        for c in cands:
            w, h = c["original"]
            parts.append(
                f"<div class=c><a href='{html.escape(c['source_url'])}' target=_blank>"
                f"<img src='{html.escape(c['thumb_url'])}' loading=lazy></a>"
                f"<small>{'<span class=f>特色图片</span> ' if c['featured'] else ''}{html.escape(c['title'])}</small>"
                f"<small>{w}×{h} · {html.escape(c['license'])} · {html.escape(c['author'][:40])}</small></div>")
        parts.append("</div>")
    return "".join(parts)


# 标志性地点和搜索词（只用来找候选；选哪张由人决定）
DEFAULT_QUERIES = {
    "eiffel-tower": "Eiffel Tower", "louvre": "Louvre Pyramid", "notre-dame": "Notre-Dame de Paris",
    "arc-de-triomphe": "Arc de Triomphe", "montmartre-sacre-coeur": "Sacré-Cœur Basilica",
    "orsay": "Musée d'Orsay", "versailles": "Château de Versailles", "palais-garnier": "Palais Garnier",
    "pont-alexandre-iii": "Pont Alexandre III", "tuileries-garden": "Jardin des Tuileries",
    "luxembourg-garden": "Jardin du Luxembourg", "place-des-vosges": "Place des Vosges",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates", action="store_true", help="只找候选并生成一览页，不下载")
    ap.add_argument("--places", default="", help="只处理这些地点（逗号分隔）")
    ap.add_argument("--sheet", default=str(Path(tempfile.gettempdir()) / "hero-candidates.html"))
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()
    only = {p for p in args.places.split(",") if p}
    headers = {"User-Agent": UA}
    with httpx.Client(headers=headers) as client:
        fetcher = Fetcher(client)
        if args.candidates:
            rows: dict[str, list[dict]] = {}
            for pid, query in DEFAULT_QUERIES.items():
                if only and pid not in only:
                    continue
                rows[pid] = fetcher.candidates(f"{query} Paris" if pid != "versailles" else query, args.limit)
                print(f"{pid}: {len(rows[pid])} 张候选", flush=True)
            Path(args.sheet).parent.mkdir(parents=True, exist_ok=True)
            Path(args.sheet).write_text(sheet_html(rows), encoding="utf-8")
            print(f"候选图一览：{args.sheet}")
            return 0
        picks = {k: v for k, v in load_picks().items() if not only or k in only}
        if not picks:
            print(f"{PICKS_PATH} 里还没有挑好的图。先跑 --candidates 看候选，再把选中的文件名写进去。")
            return 1
        today = date.today()
        out: dict[str, dict] = {}
        failed = 0
        for pid, pick in picks.items():
            try:
                img = fetcher.fetch_pick(pid, pick["title"], today=today)
            except (HeroError, httpx.HTTPError) as exc:
                failed += 1
                print(f"✗ {pid}：{exc}")
                continue
            out[pid] = {"image": json.loads(img.model_dump_json())}
            print(f"✓ {pid}：{img.title}（{img.width}×{img.height}，{img.license}）")
        HERO_PATH.write_text(json.dumps({"city": "paris", "places": out}, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"写入 {HERO_PATH}（{len(out)} 个，{failed} 个失败）")
        return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
