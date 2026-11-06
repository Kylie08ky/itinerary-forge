# web —— AI 旅行规划前端

React 18 + TypeScript + Vite + react-router + Leaflet（国内行程的地图用高德 JS API，没配 Key 自动退回 OpenStreetMap）。

| 路由 | 页面 |
|---|---|
| `/` | 首页：一句话填表、下一站倒计时 |
| `/new` | 新建行程：目的地（可多座城市）、日期、交通、住宿、出行人、主题或深度路线 |
| `/tasks/:taskId` | 生成进度（可离开，回「我的行程」找回） |
| `/plans` | 我的行程：正在生成 / 即将到来 / 已结束 / 已归档 |
| `/plans/:planId` | 行程页：封面、目录、每天时间线、地图、编辑与版本、预算 / 天气 / 随行；手机上右下角「地图」打开底部抽屉 |
| `/s/:token` | 只读分享页 |
| `/today` | 出行中的「今天」 |
| `/daytrips` · `/culture` | 周边一日游 · 了解巴黎（参考页） |
| `/usage` · `/about` | 模型与高德用量 · 版本足迹 |

页面在 `src/pages/`，组件在 `src/components/`，接口封装在 `src/api.ts`。

## 跑起来

先启动后端（在上一级目录）：

```bash
..\.venv\Scripts\python -m uvicorn app.api:app --port 8137
```

再启动前端：

```bash
npm install
npm run dev          # http://localhost:5173
```

后端地址通过 `VITE_API_BASE` 配置（默认 `http://localhost:8137`），复制 `.env.example` 为 `.env.local` 可改。

## 构建

```bash
npm run build        # tsc --noEmit + vite build -> dist/
```

浏览器验收在项目根目录：`.venv\Scripts\python -m pytest -m e2e tests/e2e`（自动起独立的前后端和临时数据库）。
