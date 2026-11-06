# 03 数据模型

> 状态（2026-11-06）：下面第 2 节是最初的设计草案；**第 4 节「已落地的实体」是实际代码里的模型**，以 Pydantic 模型为准。账号、协作、投票、偏好记忆（`User` / `TripMember` / `Poll` / `PreferenceMemory`）暂缓，没有实现。

## 1. 总览

```
User ─┬─ TripMember ─ Trip ─┬─ Day ─ Item ──► Place（地点库，全局）
      │                     ├─ CandidateEntry ──► Place
      │                     ├─ Reservation / Expense / PackingItem / Todo / Note / Attachment
      │                     ├─ Version（行程快照）
      │                     ├─ ShareLink / Invite
      │                     └─ Poll / Vote / Message
SourceMaterial ─► ExtractedPlace ─► CandidateEntry
ThemePack ─► ThemeMember ─► Place
Task（异步生成）
PreferenceMemory（用户偏好记忆）
```

## 2. 核心实体

### Place（已实现）
`id, name_zh, name_en, category, zone, area, lat, lng, duration_min, price_eur, best_time, closed_weekdays, typical_hours, long_term_closed, booking{required, lead_time_hint}, official_url, metro, highlights, note, interests, hours_verified, coords_verified`

计划新增：
| 字段 | 说明 |
|---|---|
| `source` | `curated` / `note` / `user` / `imported`，笔记和用户添加的地点要和策展地点区分 |
| `owner_id` | 用户添加的地点归属；策展地点为空 |
| `verification` | `{url: {ok, checked_at, final_url, title}, coords: {ok, checked_at, source}, hours: {...}}`，页面据此展示「已核对」 |
| `google_place_id` / `amap_poi_id` | 外部 ID，用于刷新开放时间 |
| `photos` | 图片来源和授权信息（Wikimedia 等）——已实现为独立文件 `data/places/media.json`（M02 S7b-6），按地点编号合并到接口的 `media` 字段，不进 `Place` 模型 |
| `description_zh` | 简介，标注来源——同上，在 `media.json` 的 `intro` 里 |

### ThemePack（已实现）
`id, name_zh, tagline, description, recipe{anchors_per_day{relaxed,balanced,intense}, best_time_bias, max_big_venues_per_day, notes}, members[{place_id, role(anchor|support), weight}], tips`

### Trip
`id, title, destinations[], start_date, end_date, party_size, pace(relaxed|balanced|intense), budget_level, theme_mix[{theme_id, weight}], hotel_area / hotel_place_id, arrival_at, departure_at, restrictions[], status(active|archived), owner_id, current_version, created_at`

### CandidateEntry
`trip_id, place_id, weight(1-5), flag(normal|must|avoid), added_by, source_material_id?, votes{user_id: +1/-1}, note`

### Day / Item
- `Day`：`trip_id, date, title, city, notes_md`
- `Item`：`day_id, kind(place|transfer|meal|note|reservation), place_id?, start_time, duration_min, order, locked, transport{mode, minutes, estimated}, cost?, notes`
- 排程产物先是 `ScheduleDraft`（纯数据），经质检后落库为 Day / Item。

### Version
`trip_id, number, trigger(initial|nl_revision|manual_save|restore|auto_repair), note, snapshot(完整行程 JSON), change_summary, errors_before, errors_after, created_by, created_at`

### Task
`id, trip_id?, kind(generate|revise|import_notes), status(queued|running|done|failed), steps[{name, status, started_at, finished_at, detail}], result_ref, error, created_at`

### Reservation
`trip_id, type(门票|餐厅|演出|交通|住宿|其他…), title, place_id?, date_time, status(待订|已订|已取消), confirmation_code, official_url, lead_time_hint, book_by(算出来的最晚订票日), attachments[], cost`
- 交通类支持多段（`legs[]`，含出发 / 到达地与时区）；住宿类有 `check_in` / `check_out`，跨多天显示。

### Expense
`trip_id, title, category, amount_cents(整数), currency, fx_rate_frozen, paid_by[{user, amount}], split[{user, share}], date, item_id?`

### PackingItem / Todo / Note / Attachment
- `PackingItem`：`trip_id, list, category, name, assignee?, packed`
- `Todo`：`trip_id, title, assignee?, due, priority, remind_before?, done`
- `Note`：`trip_id, day_id?, body_md, icon, color`
- `Attachment`：`trip_id, target_type(trip|place|day|reservation), target_id, filename, size, path, deleted_at?`

### SourceMaterial / ExtractedPlace
- `SourceMaterial`：`trip_id, kind(note_text|note_url|xhs_collection|gpx|kml|gmaps_list|booking_email), raw_ref, fetched_at, status`
- `ExtractedPlace`：`material_id, mention_text, resolved_place_id?, confidence, geocode_result, review_state(pending|accepted|rejected)`
- 抽取出来的地点必须经过地理编码验证，并由用户在「确认页」接受后才进入候选池。

### 协作与分享
`TripMember(trip_id, user_id, role: owner|editor|commenter|viewer)`、`Invite(token, trip_id, expires_at, max_uses)`、`ShareLink(token, trip_id, scope=read_only, expires_at)`、`Poll(options, votes)`、`Message(trip_id, user_id, body, reply_to, reactions)`

### PreferenceMemory
`user_id, tag, weight, last_seen, decay`；召回时取 Top-K，权重按天衰减，低于阈值遗忘。

## 3. 存储

- 开发期：SQLite 单文件 + `data/files/`（附件）。
- 地点库和主题包仍是 JSON 文件（版本控制友好，便于审阅），启动时装载并校验；用户添加的地点进数据库。
- 迁移：Alembic（具体 ORM 见 [02-architecture](02-architecture.md#7-待验证清单动手前各做一个小验证)）。
- 现有 `data/plans/<id>/` 的文件版本库在 v0.1 迁入 SQLite，提供一次性迁移脚本。

## 4. 已落地的实体（2026-11-06，以代码为准）

### Place（`app/library.py`，数据在 `data/places/<城市>.json`）
在第 2 节的字段之外，已经有：
| 字段 | 说明 |
|---|---|
| `setting` | 室内 / 室外 / 半室外（策展数据，雨天联动用） |
| `zone` | `core`（主城区）/ `day_trip`（要独占大半天或一天的郊区） |
| `name_local` + `name_local_check` | 当地语言名和核对记录（来源 OSM / 维基数据 / 人工、核对日期）；两者必须同时有或同时没有 |
| `address`、`metro`、`official_url` + `url_status` + `verified_at` | 地址；有官网链接就必须有核对记录 |
| `price_eur`、`price_source_url`、`price_checked_at`、`price_periods`、`concessions` | 门票价格必须带来源和日期；淡旺季、欧盟居民 / 年龄 / 学生优惠（带来源） |
| `open_access` | 公共开放空间（广场、桥、免费公园）：不收门票，预算里不算「待补」 |
| `closed_weekdays`、`closed_dates`、`date_notes`、`long_term_closed`、`typical_hours` | 每周闭馆日、每年固定闭馆的 MM-DD、那天的提醒、长期闭馆（整修 / 烧毁）、典型开放时间 |
| `season` | **当季活动的举办期**：`start / end / source_url / checked_at / confirmed_by(official\|listing) / note`；不在期内当闭馆 |
| `meal` | `lunch` / `tea`：这个点本身就是一顿午饭 / 一次下午茶，只给说明里写明的点打标 |
| `ticketing`、`booking{required, lead_time_hint, lead_days}`、`ticket_note` | 购票方式（必须线上 / 线上或现场 / 无需）；提前几天订只填说法明确的 |
| `data_status` + `draft_source` | `verified`（手工策展）/ `draft`（开放数据草稿，带 OSM / 维基数据来源和建立日期） |
| `hours_verified`、`coords_verified` | 开放时间 / 坐标有没有核对过 |

### ThemePack
`id, name_zh, tagline, description, recipe{anchors_per_day{relaxed:2, balanced:2, intense:3}, best_time_bias, max_big_venues_per_day, notes}, members[{place_id, role(anchor|support), weight}], tips`。**`anchors_per_day` 现在是白天的主要点数，所有主题一样**（晚上另算，见 [07 P20](07-polish-backlog.md)）；长期闭馆的点不能当 anchor。

### Route / Chapter / Stop / Reading / DaysAdvice（`app/routes.py`，`data/routes/<城市>.json`）
```
Route   id, name_zh, tagline, thesis, track(history|folk|food|other), base_theme,
        layout(sequential|pick-one), days{min,suggested,max}, chapters[], reading[], notes[]
Chapter id, title, story(≥20 字，带来源), sources[], stops[]
Stop    place_id, priority(core|optional), weight, look_for[], why_here, after?, sources[]   # core 站必须有 look_for / why_here / 来源
Reading title, kind(book|film), creator, year, note, source
```
`layout=pick-one` 时每章是「一类」，每天每类挑一家；路线上每一站都是当季活动时，摘要里带 `season`（限定标记）。

### Itinerary / Day / ItineraryItem（`app/schemas.py`）里和路线、故事有关的字段
`Day.chapter`（一天顶部的章标题）、`Day.guide`（导览）、`ItineraryItem.why_here`、`ItineraryItem.look_for`；`generation.route`（`RouteInfo`：路线 id、主线、每天排进来的站 `baseline`、没排进来的站、延伸阅读）用来判断你改过之后是否偏离了路线。故事和招牌**读取时现算，不存进行程**。

### Work / Sees（`app/culture.py`，`data/culture/<城市>.json` + `<城市>.auto.json`）
`Work{id, kind(film|documentary|book|podcast), title, title_zh, creator, year, summary?, sees[{place_id, note}], source, wikidata, popularity}`；`popularity` = 维基百科的语言版本数，< 20 的不收；手工策展优先于自动收录（同一部作品不重复）。

### Dish / Signature（`app/food.py`，`data/food/<城市>.json`）
`Dish{id, kind(bread|pastry|dish|drink), name_zh, zh_wiki, name_local, summary, source, wikidata, popularity, signatures[{place_id, note, source}]}`；招牌 = 某个场所卖某样东西，来源页面里必须同时有店名和菜名；名气门槛 ≥10。

### 多城市：LegPlan / LegBlock（`app/schemas.py`，规格 [11](11-multi-city.md)）
`TravelRequest.legs: list[LegPlan] | None`：多城市行程里的每座城市 = 它自己的单城市需求（`legs` 为空）+ 生成它用的主题；外层需求只记整趟的总信息（目的地 = 第一座、起止 = 整趟、交通 = 第一段去程 + 最后一段返程）。`Itinerary.legs: list[LegBlock] | None`：每座城市自己的元信息（`city_id`、`trip`、`generation`、预订清单、风险提醒），拆回单城市行程时原样取回。两个字段单城市行程都没有，老行程不变；一份行程最多 8 座城市（`MAX_LEGS`）。

### 现建城市与名气（规格 [10](10-city-builder.md)）
现建的城市和地点库放在 `data/generated/`（城市、地点、主题、图片与简介、当地名菜），精校的补充事实放在 `data/city-overlays/<城市>.json`（有来源和日期）。现建地点带 `fame`（维基百科语言版本数 + 近 12 个月浏览量），用来按名气选点；档位 `tier`：`full`（精校）/ `standard`（现建，价格 / 预约 / 部分闭馆日不核对）。

### City（`app/cities.py`，`data/cities/<城市>.json`）
时区、币种、`local_language`、城市中心、地标、建议天数、天气坐标；周边一日游的城际时间表在 `data/cities/city-pairs.json`。

### SQLite 表（`app/db.py`）
`plans`（行程 + 需求快照）· `versions`（完整行程快照 + 改动摘要）· `shares`（分享链接）· `deleted_plans`（回收站）· `reservations`（预订记录）· `budget_settings` · `fx_rates` · `packing_items` · `todos` · `day_notes`（每条安排的备注）· `attachments` · `llm_calls`（模型调用追踪）· `tasks`（后台生成任务 + 每步进度）。行程版本只存排程结果；预订、住宿、待办、清单、笔记、预算设定是用户随时改的状态，各自一张表、按行程号关联。
