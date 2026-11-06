# 规格（specs）

> 规格是事实来源，代码可以重写；**改需求先改规格**。进度只看 [05 进度清单](05-progress-checklist.md)。

## 阅读顺序

| 文件 | 内容 |
|---|---|
| [00-vision-and-scope.md](00-vision-and-scope.md) | 为什么做、给谁用、范围、原则、已做的决定（含各决定后来的结果） |
| [01-feature-inventory.md](01-feature-inventory.md) | **全部功能清单**：TREK ∪ TripStar ∪ 我们自己，每条标了决策和版本 |
| [02-architecture.md](02-architecture.md) | 技术选型（带版本号）、分层、数据流 |
| [03-data-model.md](03-data-model.md) | 数据模型 |
| [05-progress-checklist.md](05-progress-checklist.md) | **进度清单**：每轮汇报都会贴，你确认拍板后才打勾；白话版见 [`docs/progress-overview.md`](../docs/progress-overview.md) |
| [04-versions-and-roadmap.md](04-versions-and-roadmap.md) | 版本路线图，每个版本「你在前端能看到什么」 |
| [06-ui-design.md](06-ui-design.md) | 界面设计方向与验收场景 |
| [07-polish-backlog.md](07-polish-backlog.md) | **巴黎打磨**：页面结构、新建表单、排程规则（一天「上午 1、下午 1、晚上 1」）、排程评测基线、长行程验收 |
| [08-real-trips.md](08-real-trips.md) | **真实旅行驱动**：Q3 排程方案、周边一日游（斯图加特 / 亚琛）、南意圣诞（后来做成 12） |
| [09-deep-routes.md](09-deep-routes.md) | **深度游学路线**：6 条巴黎路线、两种排法、作品与故事织进行程、美食（名菜与招牌、用餐的站）、民俗体验（当季活动）、验收场景 |
| [10-city-builder.md](10-city-builder.md) | **任意目的地**：输入什么城市都能出攻略——自动认城市、现建数据（标准档）、国家弹窗选城市、名气与当地名菜 |
| [11-multi-city.md](11-multi-city.md) | **多城市旅行（K3）**：一份行程、每天标城市，换城市那天仍是一天；页内编辑、导出 |
| [12-italy-trial.md](12-italy-trial.md) | **意大利圣诞实测**：圣诞前后闭馆 / 营业提醒、城市间交通、十座热门城市精校覆盖 |
| [modules/](modules/) | 17 个板块，每个有用户故事和验收场景（M17 是 v0.5 的城市与多城市） |

做完的过程流水账（方案反复、实现细节）放在 [`docs/archive/`](../docs/archive/09-deep-routes-log.md)，不再维护；这里的文件只写**现在的要求和验收**。

## 约定

- **验收场景**用 Given / When / Then 写（假设 / 当 / 那么），并且**用你在前端能观察到的话来写**。你验收时对照场景在页面上点一遍即可。
- 每个板块末尾有「验收方式」：自动化测试、评测、前端验收，三类分开列。
- 状态图例：✅ 已有可用 · 🟡 部分有 · 🟢 数据层已落地 · ⬜ 没有。
- 功能决策图例：**做**（排进了版本）· **后做**（排进了较后版本）· **待你拍板**（我建议不做或低优先，理由写在旁边，你可以推翻）。
- 带 `P0` 的场景是该板块第一次交付必须满足的；`P1` 是同版本内的增强。
