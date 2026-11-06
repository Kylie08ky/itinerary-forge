"""读小红书链接时保护用户账号的三道闸（规格 M12 S7b）：只在用户手动点一下时读一篇，所以——

  - 同一篇缓存：同一个链接 6 小时内不重复请求；
  - 限速：两次请求之间至少隔 3 秒；
  - 连续失败就停：连续 3 次被拒 / 要求登录，暂停 10 分钟，期间不再发任何请求
    （Cookie 失效了，再试只会让账号更像机器人）。
其他网站不受影响，原样放行。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from urllib.parse import urldefrag, urlsplit

from app.notes.fetch import AccessDenied, FetchedPage, FetchError
from app.notes.sites import is_xhs


class SiteGuard:
    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
        ttl: float = 6 * 3600, max_entries: int = 50, min_interval: float = 3.0, max_failures: int = 3,
        cooldown: float = 600.0,
    ) -> None:
        self._clock, self._sleep = clock, sleep
        self.ttl, self.max_entries, self.min_interval = ttl, max_entries, min_interval
        self.max_failures, self.cooldown = max_failures, cooldown
        self._cache: OrderedDict[str, tuple[float, FetchedPage]] = OrderedDict()
        self._failures = 0
        self._blocked_until = 0.0
        self._last = -1e9
        self._lock = threading.Lock()

    def fetch(self, url: str, fetcher: Callable[[str], FetchedPage]) -> FetchedPage:
        if not is_xhs(urlsplit(url.strip()).hostname or ""):
            return fetcher(url)
        key = urldefrag(url.strip())[0]
        with self._lock:                                          # 小红书的请求一个接一个来，不并发
            now = self._clock()
            if (hit := self._cache.get(key)) and now - hit[0] < self.ttl:
                self._cache.move_to_end(key)
                return hit[1]
            if now < self._blocked_until:
                minutes = max(1, round((self._blocked_until - now) / 60))
                raise FetchError(f"小红书连续拒绝了读取，为保护你的账号先暂停 {minutes} 分钟再试；"
                                 "期间请直接粘贴笔记正文，或检查 .env 里的 XHS_COOKIE 是否还有效")
            if (wait := self.min_interval - (now - self._last)) > 0:
                self._sleep(wait)
            self._last = self._clock()
            try:
                page = fetcher(url)
            except AccessDenied:
                self._failures += 1
                if self._failures >= self.max_failures:
                    self._blocked_until, self._failures = self._clock() + self.cooldown, 0
                raise
            self._failures = 0
            self._cache[key] = (self._clock(), page)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
            return page
