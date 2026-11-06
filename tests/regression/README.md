# 回归用例集

`cases/*.json` 是冻结的固定测试集：每个文件 = 一份 request + 一份 itinerary + 期望的
质检 finding 代码集合。`tests/test_regression.py` 逐个跑 `validate_itinerary`，断言
error / warning 的代码集合和冻结值一致——既能抓「规则不再触发」，也能抓「新规则过度触发」。

| 用例 | 覆盖 |
|---|---|
| `clean-golden` | 人工校对过的行程，零 finding |
| `closed-day-louvre-tuesday` | 卢浮宫排在周二 → `closed_day` |
| `must-go-missing` | 删掉必去项 → `must_go_missing` |
| `avoid-hit-shopping` | 出现购物项 → `avoid_hit` |
| `booking-not-listed` | 需预约项不在清单 → `booking_not_listed` |
| `day-overpacked-relaxed` | relaxed 节奏塞满一天 → `day_overpacked` + `profile_style` |
| `before-arrival` | 抵达日安排早于抵达时间 → `before_arrival` |
| `wrong-day-count` | 天数与需求不符 → `day_count` |
| `repeated-attraction` | 同一个博物馆出现在两天 → `repeated_attraction` |

## 改了规则之后

若 validator 行为变化是**预期内的**：

```bash
python -m tests.regression.build_cases   # 重新冻结
git diff tests/regression/cases          # review 每个用例的期望值变化
```
