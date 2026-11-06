# AGENTS.md — AI 旅行规划 Agent

面向中国游客的国内外旅行计划生成 Agent。需求与规格见 [`specs/`](specs/README.md)，**改需求先改规格**。

## 技术栈与命令

- 后端：Python（开发环境 3.14.5，`requires-python >=3.11`）、FastAPI、Pydantic 2、httpx、Jinja2、Playwright（PDF）。
- 前端：`web/`，React 18 + TypeScript + Vite + react-router + Leaflet。
- LLM：本地 Ollama（默认 `qwen2.5:7b-instruct`），经 `app/llm.py` 的 `LLMClient` 协议，可替换。
- 测试：`.venv\Scripts\python -m pytest -q`；lint：`.venv\Scripts\ruff check .`；前端：`cd web && npm run build`；浏览器验收：`.venv\Scripts\python -m pytest -m e2e tests/e2e`（自动起独立前后端 + 临时数据库，不碰正在用的服务和数据）。
- 启动：`python -m uvicorn app.api:app --port 8137`（后端）、`cd web && npm run dev`（前端 :5173）。

## 工作方式（规格驱动）

1. 先读相关 `specs/`；新增或改动功能，先改规格，再写验收（测试 / 评测 / 前端验收场景），最后写代码。
2. 新模块动手前，先提出目录结构和选型给用户确认，不要一上来就写。
3. 用户只做三件事：用自然语言提需求、在**前端页面**验收、拍板取舍。链接是否官网、坐标、闭馆日这类能自动核对的事实，必须自己核对，不要让用户去核对 JSON 或表格。
4. 小批量提交；每次说明改了什么、有什么风险。用户没要求就不要 commit / push。
5. **每轮结尾固定三块汇报 + 进度清单**：①当前完成了什么；②需要用户在前端检查什么（写成点击步骤 + 预期现象，对应规格场景编号；这一轮没有前端可看就明说）；③下一版 / 下一批做什么。需要拍板的事单列。末尾贴 [`specs/05-progress-checklist.md`](specs/05-progress-checklist.md) 里当前版本的勾选清单（`- [x] ~~已确认~~` / `- [ ] ⏳ 待验收` / `- [ ] 🔄 进行中` / `- [ ]` 未开始）。**只有用户确认拍板后才打勾**，自己做完的标「待验收」。
6. Bug 修复先给证据（日志、失败的测试），先写能复现的失败测试，再改。
7. 每轮同时更新 [`docs/progress-overview.md`](docs/progress-overview.md)（白话版进度）和 `specs/05` 的勾选清单；做完一批、用户说「提交批次 X」才提交（不含 `tests/e2e/test_v03_media.py` 这个只有换行符差异的文件）。
8. 每轮做完用户能感受到的改动，同时在 [`data/changelog.json`](data/changelog.json)（前端「版本足迹」页）最前面追加一条：日期 + 标题 + 2–3 条白话要点，只写用户能感受到的变化，不写文件名和代号；同一天的多轮各占一条，新的在前。

## 硬规则

- **事实只信策展数据**：官网链接、闭馆日、长期闭馆、价格只来自带核对记录的策展数据；永远不信 LLM 给的网址。
- **许可证**：TripStar 是 GPL-2.0、TREK 是 AGPL-3.0，**只借思路，不拷贝代码**；TRIP 是 MIT，可参考，需署名。
- **凭证**：API Key、小红书 Cookie 只放 `.env`，不进仓库、不进聊天、不进日志。
- **外部依赖**：引入第三方包、Skill、MCP 前先审再用，固定版本，确认包真实存在（防幻觉依赖）。
- **抓取类功能**（小红书等）：先做可行性验证，并得到用户明确同意再做；必须抽成可替换的接口。
- 确定性的事（分天、路线、开放时间、预约）交给算法，LLM 只负责理解意图、抽取、表达。
- **国家的随行数据**（`data/companion/countries.json`）：由 `scripts/build_countries.py` 生成；签证只给官方页面链接（能打开才收），**不写签证政策**；插头 / 电压来自维基百科那张表。加国家先往脚本里加，再跑脚本核对。
- **自动现建的城市**（规格 10，`data/generated/`）：只能是「标准档」——价格、预约、部分闭馆日不核对就不写，写「以官网为准」；行程「注意事项」必须写明数据是自动建的。想把某座城市升成精校，往 `data/city-overlays/` 补有来源和日期的事实。
- **新增数据走批量核对脚本**：路线、作品、名菜和招牌、新地点、当季活动都是「写提纲 → 一条命令核对（来源能打开、故事里的数字和年份在来源页面里出现、坐标和官网能核对）→ 通过才写入」。核对不了的照实写「以官网为准」；**按年的数据（当季活动）带来源和核对日期，不在期内当闭馆，明年重查**；来源之间矛盾的不收。

- **错误和提示要出现在用户当前看得到的地方**：错误用页面内的弹窗（`web/src/confirm.tsx` 的 `ErrorPopup`），不要塞在页面最顶部的横幅里（页面一滚动就看不见）；面板类交互（分享等）用弹窗，不要渲染在离按钮很远的地方。
- **前端不用原生对话框**（`window.confirm / alert / prompt`）：应用内嵌的浏览器面板里 `confirm` 会瞬间返回「取消」，操作会静默失效。需要确认时用 `web/src/confirm.tsx` 的 `useConfirm()`；浏览器验收的 fixture 会在页面弹出原生对话框时让测试失败。
- **界面文字**：Avoid unnecessary secondary labels and descriptions. Let the layout do the explaining.（避免不必要的次要标签和描述，让布局本身来传达信息。）

## Windows 注意

- 项目路径含空格；PDF 用 Playwright，不要依赖系统 Edge。
- PowerShell 命令里的 `$_` 会被吞掉，复杂命令写成 Python 脚本。
- 临时克隆放短路径（如 `$TEMP`），避免 Windows 路径过长。
