# 02 架构与技术选型

> 版本号是本机实际安装的（复核过；前端依赖在 v0.3 批次 A 升级过）。标「待验证」的是还没有在本项目里跑通的选择，需要先做小验证再定。

## 1. 分层

```
┌──────────────────────────── web/ (React 18 + TS) ─────────────────────────────┐
│ 页面：首页(主题/表单) · 候选池 · 行程页(时间轴+地图+编辑) · 版本对比 · 分享页 │
└───────────────┬───────────────────────────────────────────────────────────────┘
                │ HTTP + SSE/轮询（后续 WebSocket）
┌───────────────▼─────────────── app/api.py (FastAPI) ───────────────────────────┐
│ 路由：intake · tasks · plans · places · themes · routes · culture · food · …   │
└───┬───────────┬────────────┬────────────┬────────────┬────────────┬───────────┘
    │           │            │            │            │            │
 library     scheduler    validator    renderer     importer      llm/
 地点库·主题  分天·路线    质检规则      PDF/ICS/…    笔记→地点    LLMClient
    │           │            │                                       │
    └───────────┴────────────┴─────────── providers/ ◄───────────────┘
                              地图(Google/高德/OSM) · 天气 · 汇率 · 维基
                                  │
                         storage: SQLite(+文件) → 后续 Postgres
```

模块职责与 [17 个规格板块](modules/)（外加 [09 深度路线](09-deep-routes.md)）的对应关系：

| 代码模块（2026-11-06 的实际文件） | 对应板块 |
|---|---|
| `app/library.py`（地点库、主题包、当季活动 `season`、用餐标记 `meal`）、`app/cities.py`（城市配置）、`app/names.py`（中文名 · 当地语言名）、`app/media.py` | M02、M17 |
| `app/scheduler/`：`engine.py`（主题排程：一天上午 1、下午 1、晚上 1）· `route_mode.py`（路线排程，两种排法）· `sun.py`（日落）· `geo.py`（通勤估算） | M03 |
| `app/generator.py`、`app/route_generator.py`（排程 → 骨架 → 质检 → 模型补说明）| M01 |
| `app/validator.py`、`app/judge.py`（开发者用）| M07 |
| `app/renderer.py`、`app/exports.py`（ICS / KML）、长图在 `renderer` + 接口 | M08 |
| `app/weather.py`、`app/fx.py`、`app/transit.py`（Transitous）、`app/geocode.py`（OSM）| M05、M09、M10、周边一日游 |
| `app/notes/`（笔记导入、防注入、SSRF 防护）、`app/attachments.py` | M12、M10 |
| `app/llm.py`、`app/llm_trace.py`、`app/intake_nl.py`、`app/nl_edit.py` | M01、M04、M07、M12 |
| `app/editing.py`、`app/shifting.py`、`app/versions.py`（SQLite）、`app/db.py` | M04 |
| `app/tasks.py` | M01 的异步生成 |
| `app/budget.py`、`app/booking.py`、`app/stay.py`、`app/companion.py` | M09、M06、M10 |
| **深度游学**：`app/routes.py`、`app/culture.py`、`app/food.py`、`app/daytrips.py`、`app/daytrips_live.py` | [09](09-deep-routes.md)、[08](08-real-trips.md) |
| **任意目的地与多城市**：`app/citybuild/`（认城市、现建数据、封面与简介、当地名菜）、`app/multicity.py` · `app/multiedit.py` · `app/trips.py`（一份行程按城市拆开各算各的再合回来）| [10](10-city-builder.md)、[11](11-multi-city.md)、M17 |
| **高德**：`app/maps/`（`amap.py` 搜索 / 通勤 / 定位、`coords.py` 坐标换算、`quota.py` 自留用量上限、`config.py` Key；`basemap.py` 是导出里的 OSM 街道底图，不是高德）、`app/driveest.py`（自驾估算）| M05 S7、M08 S2 |
| `app/user_places.py`（候选池里「在高德里搜」加进来的库外地点，存在 `data/generated/user-places/`）| M05 S7d |
| `app/changelog.py`（版本足迹，数据在 `data/changelog.json`）| [06](06-ui-design.md) S21 |
| 还没有：Google 地图（不做，境外用 OSM）、账号与协作（M11 / M14 暂缓）| M05、M11、M14 |

## 2. 选型

| 层 | 选择 | 版本 | 说明 |
|---|---|---|---|
| 语言 | Python | 3.14.5（`requires-python >=3.11`） | |
| Web 框架 | FastAPI / Starlette / Uvicorn | 0.141.1 / 1.6.0 / 0.52.4 | |
| 数据校验 | Pydantic | 2.13.5 | 数据文件和 API 共用同一套模型 |
| HTTP 客户端 | httpx | 0.28.1 | |
| 模板 | Jinja2 | 3.1.6 | |
| PDF | Playwright（Chromium） | 1.62.0 | 已验证；不依赖系统浏览器 |
| 测试 | pytest / ruff | 9.1.1 / 0.16.6 | |
| 前端 | React / TypeScript / Vite / react-router | 18.3.1 / 5.6.3 / 7.3.7 / 7.18.4 | 已有（v0.3 批次 A 升级过 Vite 和 react-router）|
| 地图渲染 | 境外：Leaflet + OSM 瓦片；境内：高德 JS API 2.0（自己的 Key，有自留用量上限，没配或额度到了退回 OSM） | 1.9.4 / 2.0 | 按整条路线是否都在大陆选；Google 地图不做 |
| 数据库 | SQLite（Python 自带 `sqlite3`，WAL）→ Postgres | SQLite 3.50.4 | 2026-10-09 验证：Python 3.14.5 + WAL + 多线程写入正常。不引入 ORM、不新增依赖；多人 / 账号阶段（v0.7）要上 Postgres 时再引入 SQLAlchemy，仓库接口（`PlanStore`）不变 |
| 实时 | WebSocket（FastAPI 原生） | — | v0.7 |
| 前端 E2E | Playwright（Python 版） | 1.62.0 | 用「渲染出来的页面」做验收 |
| LLM | Ollama（默认 `qwen2.5:7b-instruct`）+ 云端可替换 | — | 抽取类任务要不要用更强模型，由评测决定 |

## 3. 关键设计决定

### 3.1 地图供应商按目的地选
`MapProvider` 接口：`geocode / search_places / place_details / directions / weather`。实现：`GoogleProvider`（境外）、`AmapProvider`（境内）、`OsmProvider`（开发和降级）。**选择依据是目的地所在地区**，不是「配没配 Key」。Google 需要你自己申请 Key 并设置预算上限；Key 只放 `.env`。

### 3.2 生成是异步任务
`POST /trips` 立即返回 `task_id`；任务按步骤推进（读取地点库 → 选点 → 排程 → 质检 → 生成说明），每步写进度；前端轮询（后续可改 SSE）。关闭页面再回来，凭 `task_id` 仍能看到进度或结果。借鉴 TripStar 的做法，解决长时间推理的超时。

### 3.3 排程是纯函数
`schedule(request, pool, library, providers) -> ScheduleDraft` 不调用 LLM，输入相同则输出相同，可以被完整测试。LLM 只在两处介入：**从候选里取舍**（受候选池约束）和**写说明**（受地点库事实约束）。

### 3.4 数据文件是代码的一部分
`data/places/*.json`、`data/themes/*.json`、`data/routes/*.json`、`data/culture/*.json`、`data/food/*.json`、`data/cities/*.json` 经 Pydantic 严格校验（拒绝未知字段），加载时还检查「引用的地点都在库里、没有长期闭馆」。每个地点带 `verified_at` 和核对来源；核对由 `scripts/verify_library.py` 自动完成，不由人完成。

**批量核对脚本是数据进库的唯一路**：路线 `build_route.py`、作品 `build_culture.py` / `build_culture_auto.py`（从维基数据批量生成）、名菜和招牌 `build_food.py`、新增地点 `add_*.py` + `verify_library.py` + `apply_verification.py` + `fetch_local_names.py`。流程都是「写提纲 → 一条命令核对来源 / 数字 / 坐标 / 官网 → 通过才写入」；故事里的数字和年份必须在引用的来源页面里出现，核对不了的照实写「以官网为准」。

**按年的数据**：当季活动（`Place.season`）带举办期、来源、核对日期，行程日期不在期内当闭馆，今年过了自动失效，明年重查（`scripts/add_paris_events.py`）。

### 3.5 降级而不是失败
| 故障 | 降级行为 | 页面提示 |
|---|---|---|
| LLM 不可用 | 排程照常，说明文案用模板 | 「说明由模板生成」 |
| 路线服务不可用 | 用直线距离估算 | 「通勤时间为估算」 |
| 地图 Key 缺失 | 用 OSM 瓦片 | 无 |
| 天气接口失败 | 隐藏天气卡 | 无 |

### 3.6 版本与撤销是两套机制
- **版本**：显式的里程碑（初版、自然语言修订、手动「保存为新版本」），存完整快照 + diff，可对比、可回滚。
- **撤销**：编辑过程中的细粒度操作栈，只在内存里，刷新即丢。

## 4. 安全

依据 Day 4 的建议，落到本项目：

- **凭证**：Key、Cookie 只在 `.env` / 环境变量；日志脱敏；不进仓库。
- **抓取类功能**（笔记链接、小红书）：SSRF 防护（只允许公网 http/https、禁内网和 `file:`）、超时和大小上限、限速；先做可行性验证。
- **外部依赖**：新增包前确认真实存在、固定版本；第三方 Skill / MCP 先审再用。
- **LLM 输出不可信**：URL、价格、开放时间一律不采用；抽取出的地点要经地理编码验证后才进入候选池。
- **高风险动作需人确认**：删除行程、导入大批数据、对外分享。

## 5. 可观测性

- 每次 LLM 调用记录：用途、模型、耗时、输入输出 token、是否重试、是否降级，按任务聚合。**v0.2 已实现**：`app/llm_trace.py` 的 `TracedClient`（不存提示词原文）；「是否重试」体现为同一任务、同一用途的多次调用。
- 每个生成任务保存一份「步骤轨迹」，出错时能复盘是哪一步产生的问题。
- 后续可接 OpenTelemetry；v0.2 先用结构化日志 + 任务表。

## 6. 测试与评测金字塔

| 层 | 工具 | 覆盖 |
|---|---|---|
| 单元 | pytest | 地点库、排程、质检规则、导出 |
| 回归 | `tests/regression/` 冻结用例 | 质检输出不漂移 |
| 评测 | 标注数据集 + LLM Judge | 笔记抽取准确率、偏好匹配度、主题契合度 |
| 数据核对 | `scripts/verify_library.py`（可定时跑） | 官网链接、坐标、闭馆日 |
| 前端 E2E | Playwright | 关键流程在真实渲染页面上的断言 + 截图 |

## 7. 待验证清单（动手前各做一个小验证）

1. ~~SQLModel / SQLAlchemy 与 Pydantic 2.13、Python 3.14 的兼容性~~：已决定 v0.1 用标准库 sqlite3，见上表。
2. Google Maps Platform：需要开通哪些 API、计费方式、如何设预算上限。
3. 高德：Web 服务 Key 与 JS Key 的申请（v0.5）。
4. 本地 7B 模型做「笔记 → 地点」抽取的准确率是否够用（v0.4 前用评测集回答）。
5. 小红书笔记链接能否在不登录的情况下取到正文（v0.4 前验证，不涉及 Cookie）。
