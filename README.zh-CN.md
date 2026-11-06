# AI Travel Planner Agent

[English](README.md) · [规格](specs/README.md) · [进度](docs/progress-overview.md) · [验证说明](VALIDATION.md)

> **面向中国游客的国内外旅行计划生成系统：策展数据 + 确定性排程 + 规则质检 + 多格式交付。**
> 把「我要去某个城市玩几天」变成一份**可执行、可核对、可修改、可交付**的行程。

这不是聊天助手，是一个旅行服务交付系统：选主题（或一条深度路线）→ 算法排出每天的时间线 → 质检 → 模型只负责把说明写顺 → 导出 PDF / 日历 / 长图 / 分享链接 → 改了再重新排，留版本历史。

目的地：**任意城市**（库里没有的自动现建）；巴黎和意大利十座热门城市是精校的；国内行程用高德地图；另有斯图加特 / 亚琛周边各 7 座城市的一日游。项目按规格驱动做，需求和验收场景在 [`specs/`](specs/README.md)。

## 它和普通「AI 生成行程」有什么不同

- **确定性与概率性分离**：分天、路线、开放时间、闭馆日、午晚饭、预约、预算都由 Python 算法算（输入相同则输出相同）；模型只负责理解一句话需求、从笔记里抽地点、把说明写顺。模型关着也能出完整行程，说明改用模板。
- **事实只信策展数据**：官网链接、闭馆日、价格、当季活动日期都来自带核对记录的数据，由脚本自动核对（官网能不能打开、坐标和 OpenStreetMap 差多远、故事里的年份在不在来源页面里）。**永远不信模型给的网址**；核对不了的写「以官网为准」。
- **每个事实带来源和日期**，页面上能点开。
- **用户永远能改**：拖拽、锁定、换一个、撤销重做、一句话改行程，改完自动重新质检；每次保存是一个版本，可对比、可回滚。

## 现在能做什么

| 方面 | 内容 |
|---|---|
| 目的地 | 任意城市或国家，库里没有就自动现建；多城市旅行是**一份行程**（每天标城市），一次最多 8 座；国内用高德，可按需查真实通勤 |
| 生成 | 主题（可多选、有权重）或深度路线；首页一句话填表；后台生成，离开页面也能找回 |
| 排程 | 一天 = 上午 1、下午 1、晚上 1；避开闭馆日；午饭一定有；黄昏最佳的点按日落定时间；当季活动只在举办期里排 |
| 深度路线与文化 | 有主线、有章节的文化路线；电影 / 书的故事挂在地点上，随行程出现；名菜与招牌 |
| 编辑与版本 | 拖拽、锁定、换一个、候选池、平移日期、复制、归档、版本对比与回滚 |
| 费用与出行 | 住宿与预订、天气、预算（逐人算优惠、汇率）、随行清单、出行中的「今天」 |
| 导入 | 粘贴朋友的笔记或链接（含小红书，用你自己的登录状态），库外地点进「待核实」 |
| 交付 | PDF、长图、ICS、KML、Markdown / HTML、只读分享链接；手机可用（行程页有地图抽屉），离线只读 |

## 架构

```
 web/ (React + TS + Vite)        app/api.py (FastAPI)
 首页 · 新建 · 我的行程 · 行程页 ◄──►  /intake /tasks /plans /routes /culture /food …
                                        │
          library（地点库·主题） ─ scheduler（分天·路线） ─ validator（质检） ─ generator ─ renderer（导出）
          citybuild（现建城市） · multicity（多城市） · maps（高德） · versions（SQLite）
```

数据是代码的一部分：`data/` 下的地点、主题、路线、作品、名菜都用 Pydantic 严格校验；批量核对脚本在 `scripts/`（写提纲 → 一条命令核对 → 通过才写入）。LLM 可替换：`app/llm.py` 的 `LLMClient`，默认本地 Ollama（`qwen2.5:7b-instruct`）。

## 跑起来

前置：Python 3.11+、Node 20+；想要模型写说明就装本地 [Ollama](https://ollama.com) 并 `ollama pull qwen2.5:7b-instruct`（不装也能用）。

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows PowerShell
pip install -e ".[dev]"
playwright install chromium            # PDF 用
python -m uvicorn app.api:app --port 8137     # http://localhost:8137/docs

cd web && npm install && npm run dev          # 另开终端，http://localhost:5173
```

```bash
.venv\Scripts\python -m pytest -q                 # 单元测试
.venv\Scripts\ruff check .                        # lint
cd web && npm run build                           # 前端类型检查 + 构建
.venv\Scripts\python -m pytest -m e2e tests/e2e   # 浏览器验收（约 20–25 分钟，自动起独立前后端和临时数据库）
```

Docker：`docker compose up --build`（后端 :8137，前端 :5173）。国内行程想用高德，在 `.env` 里配自己的 Key（见 `.env.example`），不配也能用；小红书 Cookie 的配法见 [`specs/modules/M12-material-import.md`](specs/modules/M12-material-import.md)，**只放 `.env`，不要发给任何人**。

## 项目结构

| 路径 | 说明 |
|---|---|
| `app/` | 后端：`schemas.py` 数据契约 · `library.py` / `cities.py` 地点库和城市 · `scheduler/` 排程 · `validator.py` 质检 · `generator.py` 生成 · `renderer.py` / `exports.py` 导出 · `citybuild/` 现建城市 · `multicity.py` 多城市 · `maps/` 高德 · `versions.py` 版本库 |
| `web/` | React 前端（页面在 `src/pages/`，组件在 `src/components/`），见 [`web/README.md`](web/README.md) |
| `data/` | 策展数据（带核对记录） |
| `scripts/` | 数据核对、批量生成、评测脚本 |
| `tests/` | 单元测试、浏览器验收（`tests/e2e/`）、冻结回归用例 |
| `specs/` · `docs/` | 规格、进度、分支与发布规则（[`docs/release.md`](docs/release.md)） |

## 已知局限

- **精校的城市有限**：巴黎、意大利十座热门城市、北京故宫；其余是现建的「标准档」，价格 / 预约 / 部分闭馆日没核对，行程里会写明。
- **当季活动的日期按年**：今年的过了自动失效，明年要重新查。
- **模型质量上限**：本地 7B 写的说明偶尔不合格，会被检查丢掉并换成模板。
- **排程是算法，不是最优解**：午饭前后会有 1–2 小时空档，晚上的点可能挑不到。
- **无账号、无协作**：自用 / 朋友用的形态。
- **地图**：国外用免费 OpenStreetMap 瓦片（无配额保护）；国内用高德，有自留用量上限，额度到了自动退回 OpenStreetMap。

## 许可证

[MIT](LICENSE)。数据里的第三方图片和记录各自保留许可证与署名。
