# AI Travel Planner Agent

> **面向中国游客的国内外旅行计划生成系统：策展数据 + 确定性排程 + 规则质检 + 多格式交付。**
> 把「我要去某个城市玩几天」变成一份**可执行、可核对、可修改、可交付**的行程。

这不是聊天助手，是一个**旅行服务交付系统**：选主题（或一条深度路线）→ 算法排出每天的时间线 → 质检 → 模型只负责把说明写顺 → 导出 PDF / 日历 / 长图 / 分享链接 → 你改了再重新排、留版本历史。

目的地：**任意城市**（库里没有的自动现建）；**巴黎**和意大利十座热门城市是精校的，国内行程用高德地图；另有**斯图加特 / 亚琛周边各 7 座城市的一日游**。项目按规格驱动做，需求和验收场景都在 [`specs/`](specs/README.md)，当前进度的白话版见 [`docs/progress-overview.md`](docs/progress-overview.md)。

---

## 它和普通「AI 生成行程」有什么不同

- **确定性与概率性分离**：分天、路线、开放时间、闭馆日、午晚饭、预约、预算这些能算准的事全部由 Python 算法做（`app/scheduler/`，输入相同则输出相同）；模型只负责理解一句话需求、从笔记里抽地点、把每天的说明写顺——模型关着也能出完整行程，说明改用模板并在页面提示。
- **事实只信策展数据**：官网链接、闭馆日、长期闭馆、价格、当季活动日期都来自带核对记录的数据（`data/`），由 `scripts/` 下的脚本自动核对（官网能不能打开、坐标和 OpenStreetMap 差多远、故事里的年份在不在来源页面里）；**永远不信模型给的网址**。核对不了的页面照实写「以官网为准」。
- **每个事实带来源和日期**：路线的故事、作品和招牌、圣诞市场的日期，都要有来源页面，页面上能点开。
- **用户永远能改**：拖拽、锁定、换一个、删除、撤销重做、一句话改行程，改完自动重新质检；每次保存是一个版本，可对比、可回滚。

## 现在能做什么

| 方面 | 内容 |
|---|---|
| 目的地 | 输入任意城市或国家：库里没有就自动现建（维基数据 + OpenStreetMap，第一次几分钟），国家给热门城市多选；**多城市旅行是一份行程**（每天标城市，换城市那天仍是一天），可在页内直接改；国内城市用高德地图，并可按需查真实通勤时间 |
| 生成 | 主题（艺术 / 时尚 / 购物 / 娱乐 / 亲子 / 摄影 / 美食 / 博物馆，可多选、有权重）或**深度路线**；首页一句话填表（「10 月 20 号去巴黎玩 4 天，两个人，喜欢艺术和咖啡馆」）；生成在后台跑，离开页面也能在「我的行程」找回 |
| 排程 | 一天 = 上午 1、下午 1、晚上 1（轻松少一段、紧凑多一个）；避开闭馆日；午饭一定有；午餐点 / 下午茶点替代占位；黄昏最佳的点按当天日落定时间；一日游独占一天；当季活动只在举办期里排 |
| 深度路线 | 有主线、有顺序、有背景故事的文化路线（艺术、时尚、历史与思想、古建筑与名胜、法式餐桌、圣诞集市）；章节 / 导览 / 「看什么」/ 延伸阅读；两种排法（按章往下走 / 每天每类挑一家）；可自选站，天数不限制 |
| 文化与美食 | 「地点 → 作品」（电影 / 书）自动出现在行程里；名菜名点与「招牌」；「了解巴黎」参考页；全部有来源 |
| 周边一日游 | 填你的住址 → 斯图加特 / 亚琛周边 7 座城市，区分只坐区间车和需另买票的长途车；圣诞市场按举办期；「排一天」 |
| 编辑与版本 | 拖拽、锁定、换一个、候选池（必去 / 避开 / 权重）、平移日期、复制、归档、版本对比与回滚 |
| 费用与出行 | 住宿与预订（已订门票自动锁定入馆时间）、天气（下雨提示室内备选）、预算（逐人算优惠、汇率、超预算自动换便宜的）、随行清单（行李 / 待办 / 备注 / 文件附件）、出行中的「今天」卡片 |
| 导入 | 粘贴朋友的笔记或链接（含小红书，用你自己的登录状态）→ 自动找出地点并标出处；库外地点进「待核实」 |
| 交付 | PDF、长图、日历（ICS）、地图（KML）、Markdown / HTML、只读分享链接；手机可用（行程页有「地图」底部抽屉，返回键只关抽屉），离线只读；「版本足迹」页记录每次更新了什么 |
| 质检 | 确定性规则（开放时间、重复、抵达 / 离开缓冲、每日活动量…）+ 排程评测基线 + 开发者用的 LLM Judge |

## 架构

```
 web/ (React 18 + TS + Vite)            app/api.py (FastAPI)
 首页 · 新建 · 我的行程 · 行程页   ◄──►  /intake /tasks /plans /routes /culture /food /daytrips …
 今天 · 了解巴黎 · 周边一日游 · 分享页          │
                                  ┌────────────┼─────────────────────────────────────────┐
                              library       scheduler/engine         scheduler/route_mode
                     地点库 · 主题 · 当季活动   主题 → 每天时间线        深度路线 → 每天时间线
                                  │            │                          │
                              culture · food   validator（质检）     route_generator（导览，模型可选）
                              routes · daytrips      │
                                  └──────── generator ─ renderer（MD / HTML / PDF / ICS / KML / 长图）
                                                 │
                                  versions（SQLite）· tasks · budget · companion · notes · attachments
                                  providers：天气（Open-Meteo）· 汇率（Frankfurter）· 公共交通（Transitous）· 地理编码（OSM）· 高德（国内地图、搜索、通勤；自己的 Key）
```

- **数据是代码的一部分**：`data/places/*.json`、`data/themes/*.json`、`data/routes/*.json`、`data/culture/*.json`、`data/food/*.json` 都用 Pydantic 严格校验（拒绝未知字段）；加载时检查「地点都在库里、没有长期闭馆」。
- **批量核对脚本**：`scripts/build_route.py`（路线）、`build_culture.py` / `build_culture_auto.py`（作品，后者从维基数据批量生成）、`build_food.py`（名菜和招牌）、`verify_library.py`（官网 / 坐标 / 闭馆日）、`fetch_local_names.py`（当地语言名）、`add_*.py`（新增地点、当季活动）。写提纲 → 一条命令核对 → 通过才写入。
- **LLM 可替换**：`app/llm.py` 的 `LLMClient` 协议；默认本地 Ollama（`qwen2.5:7b-instruct`，笔记导入用 14B）。模型的输出在进入行程前逐条检查：出现网址、价格、钟点、路线外的名字就整段丢掉。
- **版本库是 SQLite**（`PlanStore`），每次保存一个完整快照 + 改动摘要。

## 跑起来

前置：Python 3.11+、Node 20+；想要模型写说明就装本地 [Ollama](https://ollama.com) 并 `ollama pull qwen2.5:7b-instruct`（不装也能用，说明改用模板）。

```bash
# 后端
python -m venv .venv
.venv\Scripts\activate                 # Windows PowerShell
pip install -e ".[dev]"
playwright install chromium            # PDF 用；不装会退回系统 Chrome/Edge
python -m uvicorn app.api:app --port 8137     # http://localhost:8137/docs

# 前端（另开一个终端）
cd web && npm install && npm run dev          # http://localhost:5173
```

测试和检查：

```bash
.venv\Scripts\python -m pytest -q             # 单元 2013 项（2026-11-06）
.venv\Scripts\ruff check .                    # lint
cd web && npm run build                       # 前端类型检查 + 构建
.venv\Scripts\python -m pytest -m e2e tests/e2e   # 浏览器验收 473 项（约 20–25 分钟）
```

浏览器验收默认**自动起一套独立的后端 + 前端 + 临时数据库**（不碰你平时用的服务和数据，跑完删除）；想对着已经在跑的服务测：`E2E_USE_RUNNING=1 …`，或设 `E2E_API` / `E2E_WEB`。整套并发跑得久时有几条对时序敏感的用例偶尔失败，单独重跑通过。

### 命令行（不开前端）

```bash
python -m app.cli plan data/personas/persona-01-museum-foodie.json --repair --save --render
python -m app.cli revise <plan_id> "第 2 天上午加一个咖啡馆" --render
python -m app.cli history <plan_id>
python -m app.cli plan <req.json> --backend stub    # 不调模型，用内置样例
```

### Docker

```bash
docker compose up --build     # 后端 :8137，前端 :5173；浏览器打开 http://localhost:5173
```

后端通过 `host.docker.internal:11434` 连宿主机的 Ollama；没有 Ollama 也能用。PDF 用镜像里 Playwright 自带的 Chromium（中文字体 fonts-noto-cjk），后端以非 root 用户运行并带健康检查；行程数据库在命名卷 `plan-store`，`docker compose down` 不会丢。地图用免费的 OpenStreetMap，不需要任何 Key；国内行程想用高德地图，在 `.env` 里配自己的高德 Key（见 `.env.example`），不配也能用。

### 首页一句话填表 + 封面大图

首页首屏是实拍大图 + 毛玻璃提示卡：写一句话，点「开始规划」，`POST /intake/nl` 读懂意图并把表单填好（**不会自动生成**）。日期、天数、人数、预算、地点由规则读，本地模型只补主题映射 / 节奏 / 特别要求；模型没开也能读。评测：`python scripts/eval_intake.py`。大图在 `data/places/hero/`（Wikimedia Commons，署名在页面角落），换图用 `scripts/fetch_hero_images.py`。

### 文件附件

行程页「随行」卡的「文件」页签：上传 PDF / PNG / JPG（单个 ≤ 10MB，一个行程最多 50 个），可挂到整个行程 / 某一天 / 某个地点 / 某条预订。文件只存在你自己的电脑上（`data/plans/attachments/<行程id>/`，已被 git 忽略），按文件内容判断类型，不进分享页和任何导出；删除先进回收站，30 天后自动清理。

### 导入小红书笔记链接（可选）

「从笔记导入 → 粘贴链接」能读普通网页；小红书笔记要登录才看得到，所以要把**你自己的**登录 Cookie 填进 `.env`：

1. 电脑浏览器打开 `www.xiaohongshu.com` 并登录（建议用小号：用户协议不允许自动化访问，账号有被限流的可能）。
2. `F12` → 「网络 / Network」→ `F5` 刷新 → 点最上面一项 → 「标头 / Headers」→「请求标头」里找到 `Cookie` → **右键 →「复制值」**。
3. 在项目目录运行 `.venv\Scripts\python scripts\set_xhs_cookie.py`，粘贴后回车（**屏幕上不显示字符，这是正常的**）。它把 Cookie 写进 `.env` 的 `XHS_COOKIE=`，只回显「已配置：共 N 项」，不显示值；`--check` 看配好了没有。`.env` 已被 git 忽略，**不要发给任何人，包括聊天里**。
4. Cookie 一般一到四周失效；失效后导入弹窗会提示，重新复制一次即可。

Cookie 只会发给 `xiaohongshu.com`，不写日志、不进行程、不被任何接口返回；每次只在你点「读取并解析」时读一篇，同一个链接 6 小时内不重复请求，请求之间隔 3 秒，连续 3 次被拒自动暂停 10 分钟。

## 项目结构

| 路径 | 说明 |
|---|---|
| `AGENTS.md` | 给 AI 助手的工作规则（规格驱动、硬规则） |
| `specs/` | 规格：愿景、功能清单、架构、数据模型、路线图、**进度清单**、界面设计、打磨清单、真实旅行、深度路线（[索引](specs/README.md)）；`specs/modules/` 17 个板块 |
| `docs/progress-overview.md` | 进度总览（白话版）；`docs/reports/` 评测报告；`docs/model-eval.md` 本地模型对比 |
| `app/schemas.py` | `TravelRequest` / `Itinerary` 数据契约 |
| `app/library.py` · `app/cities.py` | 地点库、主题包、当季活动；城市配置 |
| `app/citybuild/` · `app/multicity.py` · `app/maps/` | 任意目的地现建城市、多城市（一份行程按城市拆开各算各的）、高德接入（搜索 / 通勤 / 坐标换算 / 用量保护；导出的街道底图是 OSM） |
| `app/scheduler/` | `engine.py`（主题排程）· `route_mode.py`（深度路线排程）· `sun.py`（日落）· `geo.py` |
| `app/generator.py` · `app/route_generator.py` | 主题 / 路线 → 行程（骨架 → 质检 → 模型补说明） |
| `app/routes.py` · `app/culture.py` · `app/food.py` · `app/daytrips*.py` | 深度路线、作品与故事、名菜与招牌、周边一日游 |
| `app/validator.py` · `app/judge.py` | 确定性质检；开发者用的 LLM Judge |
| `app/editing.py` · `app/nl_edit.py` · `app/versions.py` | 编辑、一句话改行程、版本库（SQLite） |
| `app/renderer.py` · `app/exports.py` | Markdown / HTML / PDF / 长图；ICS / KML |
| `app/budget.py` · `app/booking.py` · `app/companion.py` · `app/weather.py` · `app/fx.py` | 预算、预订、随行清单、天气、汇率 |
| `app/notes/` · `app/attachments.py` | 笔记导入（防注入、SSRF 防护）、文件附件 |
| `app/api.py` · `app/cli.py` · `app/tasks.py` | HTTP / 命令行入口、后台生成任务 |
| `web/` | React + TS 前端（页面在 `web/src/pages/`，组件在 `web/src/components/`） |
| `data/places` `themes` `routes` `culture` `food` `seasonal` `cities` | 策展数据（带核对记录） |
| `scripts/` | 数据核对、批量生成、评测脚本 |
| `tests/` · `tests/e2e/` · `tests/regression/` | 单元测试、浏览器验收、冻结回归用例；`tests/fixtures/` 里是排程评测基线和 K0 基线 |

## 已知边界与局限

- **精校的城市有限**：巴黎、意大利十座热门城市（景点补全、官网闭馆日脚本核对）、北京故宫；周边一日游目的地是开放数据草稿，价格和开放时间出发前要到官网确认。其余城市都是现建的「标准档」（见下）。
- **当季活动的日期是按年的**：目前只有巴黎冬季两个有依据的圣诞市场（日期来自多个聚合网站、官方网站没能打开）；今年的过了自动失效，明年冬天要重新查。杜乐丽、拉德芳斯等来源互相矛盾的没收。
- **模型质量上限**：本地 7B 写的说明偶尔不合格，会被检查丢掉并换成模板；模型生成文案的好坏我在没有开模型的环境里没法实测。
- **排程是算法，不是最优解**：一天按「上午 1、下午 1、晚上 1」排，不补小点，午饭前后会有 1–2 小时空档；晚上的点可能挑不到而空着。
- **现建城市是「标准档」**：库里没有的城市会自动现建（维基数据 + OpenStreetMap，第一次几分钟），价格 / 预约 / 部分闭馆日没核对，行程里会写明「数据是自动建的」；国内小城的景点靠开放数据，解放碑这类找不到的要在候选池里点「在高德里搜」。
- **长行程**：主题里的点排完后（巴黎单主题约 14 天起）会用别的主题的代表性景点补，巴黎的点最多排满约 22 天，更长的旅行可以拆成几座城市（多城市旅行：新建页「添加下一座城市」，生成**一份**行程、每天标城市，见 [specs/11](specs/11-multi-city.md)；一次最多 8 座城市）。
- **无账号、无协作、无限流**；`/plans` 对所有能访问服务的人可见（自用 / 朋友用的形态）。
- **地图**：国外用免费 OpenStreetMap 瓦片（没有配额保护）；国内用高德，有自留的用量上限，额度到了或没配 Key 自动退回 OpenStreetMap。高德服务协议不许缓存，真实通勤时间点了才查、不保存。
- 小红书抓取只读你主动粘贴的链接，用你自己的登录状态，账号有被限流的风险。

## 下一步

见 [`docs/progress-overview.md`](docs/progress-overview.md)：交付收尾（演示脚本、评测报告、自托管说明）；GPX 导出与 KML / GPX 导入、航班补全、协作与账号、AI 伴游、小红书合集暂缓。
