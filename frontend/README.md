# ChemAgent Frontend

React + TypeScript 控制台，用于创建、观察和测试 Research Preview 与 Full Campaign。前端只通过 `/api/v1` 与后端通信，不读取仓库内的运行文件。

## 本地运行

```bash
cd frontend
npm install
npm run dev
```

默认地址为 `http://127.0.0.1:5173`。Vite 将 `/api` 代理到 `http://127.0.0.1:8000`；可在 `.env.local` 覆盖：

```bash
VITE_API_BASE_URL=
VITE_API_PROXY_TARGET=http://127.0.0.1:8000
```

若前后端部署在不同域名，将 `VITE_API_BASE_URL` 设为后端地址，例如 `http://127.0.0.1:8000`。

## 新建任务选项

- `Online literature`：聚合学术 API，并开放 Agent 的论文搜索工具。
- `Web Search`：启用通用网页搜索和网页读取，可独立于论文检索运行。
- `Download PDFs`：从开放来源自动切换下载并解析全文，仅在 Online literature 开启时可用。

`Research Preview` 不调用 LLM；`Full Campaign` 还会执行设备映射并需要后端 LLM 配置。所有 API Key 都保存在仓库根目录的后端 `.env`，不要写入 `frontend/.env.local` 或浏览器请求。后端当前无鉴权，仅用于绑定 `127.0.0.1` 的本地测试。

## 检查命令

```bash
npm run build
npm run lint
npm test
npm run test:e2e
```

Playwright 测试会 mock 后端 API，同时覆盖桌面 Chromium 和 Pixel 7 视口。
本机只安装了 Google Chrome 时可运行 `PLAYWRIGHT_CHANNEL=chrome npm run test:e2e`。

## API 依赖

- `GET /api/v1/health`
- `GET|POST /api/v1/campaigns`
- `GET /api/v1/campaigns/:id`
- `GET /api/v1/campaigns/:id/events`
- `POST /api/v1/campaigns/:id/cancel`
- `POST /api/v1/campaigns/:id/observations`
- `POST /api/v1/uploads`
- `GET /api/v1/assets/workstation-map`

SSE 同时兼容默认 `message` 与命名 `campaign` 事件；连接失败时保留 4 秒详情轮询和 5 秒列表轮询。
