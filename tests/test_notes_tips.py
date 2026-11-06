"""M12 S4：笔记提纯，只靠规则。"""

import pytest

from app.library import load_library
from app.notes.models import TipKind
from app.notes.tips import extract_tips, is_complaint


@pytest.fixture(scope="module")
def lib():
    return load_library("paris")


def kinds(tips):
    return {t.kind: t for t in tips}


def test_booking_queue_and_closed_from_the_spec_example(lib):
    tips = extract_tips("上午奥赛博物馆，周一闭馆别走空！建议提前订票，不然排队2小时+。", lib.get_place("orsay"))
    k = kinds(tips)
    assert k[TipKind.booking].label == "需要提前订票 / 预约"
    assert k[TipKind.queue].label == "排队约 2 小时以上"
    assert k[TipKind.closed].label == "周一闭馆"
    assert k[TipKind.closed].conflict is None                  # 地点库里奥赛正是周一闭馆
    assert TipKind.duration not in k                           # 「排队 2 小时」不是游玩时长
    assert "订票" in k[TipKind.booking].text


def test_closed_day_that_disagrees_with_the_library_is_flagged(lib):
    tip = kinds(extract_tips("凡尔赛宫记得周二不开，整个行程要留一天", lib.get_place("versailles")))[TipKind.closed]
    assert tip.label == "周二闭馆"
    assert tip.conflict is None or "以地点库为准" in tip.conflict
    wrong = kinds(extract_tips("奥赛周二闭馆", lib.get_place("orsay")))[TipKind.closed]
    assert "笔记说周二" in wrong.conflict and "周一" in wrong.conflict and "以地点库为准" in wrong.conflict


def test_closed_day_for_a_place_with_no_recorded_closure(lib):
    tip = kinds(extract_tips("周一闭馆", lib.get_place("eiffel-tower")))[TipKind.closed]
    assert tip.conflict and "地点库没有记闭馆日" in tip.conflict


@pytest.mark.parametrize("text, label", [
    ("排队40分钟但值得", "排队约 40 分钟"),
    ("排队约1.5小时", "排队约 1.5 小时"),
    ("排队两小时", "排队约 2 小时"),
])
def test_queue_labels(text, label):
    assert kinds(extract_tips(text))[TipKind.queue].label == label


@pytest.mark.parametrize("text, label", [
    ("Shakespeare and Company 书店很值得逛，30分钟够了", "建议约 30 分钟"),
    ("建议2小时看精华", "建议约 2 小时"),
    ("至少留3个小时", "建议约 3 小时"),
    ("逛1.5小时左右", "建议约 1.5 小时"),
])
def test_duration_labels(text, label):
    assert kinds(extract_tips(text))[TipKind.duration].label == label


@pytest.mark.parametrize("text, has", [
    ("卢浮宫一定要订时段票", True),
    ("要提前预约", True),
    ("需要预约", True),
    ("不用预约直接进", False),
])
def test_booking_detection(text, has):
    k = kinds(extract_tips(text))
    assert (TipKind.booking in k) is True
    assert (k[TipKind.booking].label != "无需预约") is has


def test_cash_safety_and_price():
    k = kinds(extract_tips("只收现金，小心扒手，人均35欧"))
    assert k[TipKind.cash].label == "只收现金"
    assert k[TipKind.safety].label.startswith("小心扒手")
    assert k[TipKind.price].label == "人均 35 欧"


def test_no_tips_in_plain_text():
    assert extract_tips("这家店很好看，我很喜欢") == []


def test_at_most_one_tip_per_kind():
    tips = extract_tips("周一闭馆，周二也不开。排队1小时，排队2小时")
    assert len({t.kind for t in tips}) == len(tips)


@pytest.mark.parametrize("text, yes", [
    ("人多东西贵，拍个照就行 避雷", True),
    ("其实一般，纯打卡", True),
    ("红磨坊——纯游客，门票贵", True),
    ("别买这里的小贩东西", True),
    ("避坑指南：提前订票", False),                    # 「避坑」是教你怎么避开，不是吐槽
    ("很值得去，超级推荐", False),
])
def test_complaint_cues(text, yes):
    assert is_complaint(text) is yes


@pytest.mark.parametrize("text, label", [
    ("坐地铁火车2.55€一人", None),                       # 光有金额：可能是地铁票，不标成价格
    ("一杯咖啡3.5欧", None),
    ("我当时买的一人84€", "约 84 欧"),
    ("门票17欧", "门票 17 欧"),
    ("人均35欧", "人均 35 欧"),
])
def test_prices_need_a_cue_word(text, label):
    got = kinds(extract_tips(text)).get(TipKind.price)
    assert (got.label if got else None) == label


@pytest.mark.parametrize("text", ["官网预约好免费票后凭票进入", "按照预约的时间到", "网上预约最满的橘园"])
def test_booking_phrased_as_a_fact_is_also_detected(text):
    assert TipKind.booking in kinds(extract_tips(text))
