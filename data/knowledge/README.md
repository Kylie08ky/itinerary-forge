# 巴黎知识库

最初拷贝自 `../../00-dify-knowledge-base-demo/data/`（快照，非软链）；之后在这里独立维护，不再和 00 项目同步。

| 文件 | 用途 |
|---|---|
| `paris-planning-brief.md` | 规划要点简报（从下面几份 SOP 提炼的确定性事实：闭馆日、预约、通票、交通、节奏）；`app/planner.py` 的 `load_knowledge` 读它，作为模型规划时的上下文，每座城市一份 `<城市>-planning-brief.md` |
| `place-aliases.json` | 地点别名分组和每周闭馆日；质检（`app/validator.py`）和地点库（`app/library.py`）读它 |
| `louvre-reservation-sop.md` | 卢浮宫预约规则 → 影响订票清单 |
| `paris-metro-sop.md` | 地铁 / RER 换乘、票种 → 影响交通衔接检查 |
| `paris-museum-pass-sop.md` | 博物馆通票覆盖范围与省钱逻辑 |
| `refund-and-change-sop.md` | 退改政策 → 影响风险提醒 |
| `common-travel-faq.md` | 通用问答 |
| `sources-index.csv` | 上面几份资料的来源索引 |
| `<城市>-attractions.json` | 景点白名单：`app/planner.py` 的 `attraction_catalog` / `format_attractions` 读它（模型直接规划的旧路径和 `/attractions` 接口）；主流程的地点库在 `data/places/` |
