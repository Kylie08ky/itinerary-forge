"""新建页（/new）的共用填法：交通是必填项（规格 07 P07），所以每个要点「生成行程」的测试都得先填来回交通。"""

from __future__ import annotations

from datetime import datetime, timedelta

from playwright.sync_api import Page


def fill_trip(page: Page, start: str | None = None, end: str | None = None, *,
              arrive: str = "10:30", leave: str = "18:00") -> tuple[str, str]:
    """填开始 / 离开日期（不给就用页面默认值）和来回交通（飞机；到达 start 当天 arrive、返程 end 当天 leave 出发）。
    返回 (start, end)。"""
    if start:
        page.get_by_label("开始日期").fill(start)
    if end:
        page.get_by_label("离开日期").fill(end)
    start = page.get_by_label("开始日期").input_value()
    end = page.get_by_label("离开日期").input_value()
    arrive_at = datetime.fromisoformat(f"{start}T{arrive}")
    page.get_by_label("去程出发时间").fill((arrive_at - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"))
    page.get_by_label("去程到达时间").fill(arrive_at.strftime("%Y-%m-%dT%H:%M"))
    page.get_by_label("返程出发时间").fill(f"{end}T{leave}")
    return start, end
