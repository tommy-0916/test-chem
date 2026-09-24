# Chem Agent Web Backend

这是给 GitHub Pages 前端使用的安全 FastAPI 网关。它默认不暴露真实 agent，只提供一个保守的 `/api/run-agent` 接口骨架。

## 安全设计

- 必须携带访问码：`X-Access-Code`
- CORS 只允许指定前端来源
- 上传文件限制大小和扩展名
- 同一 IP 默认每分钟最多 3 次请求
- 默认同一时间只执行 1 个任务
- agent 默认 180 秒超时
- 上传文件只放临时目录，请求结束后自动删除
- FastAPI 建议只监听 `127.0.0.1`，由 Nginx 反向代理到 HTTPS

## 本地运行

```bash
cd /path/to/chem-agent
python -m venv .venv
source .venv/bin/activate
pip install -r web_backend/requirements.txt

export WEB_ACCESS_TOKEN="换成一串很长的随机访问码"
export ALLOWED_ORIGINS="https://echo-hyt.github.io"

uvicorn web_backend.app:app --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## Nginx 反向代理建议

不要把 `8000` 端口直接暴露给公网。公网只开放 `80/443`，由 Nginx 转发到本机 FastAPI。

```nginx
server {
    listen 443 ssl http2;
    server_name agent.yourdomain.com;

    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 240s;
    }
}
```

前端的 API 地址填写：

```text
https://agent.yourdomain.com/api/run-agent
```

## 接入真实 agent

真实调用逻辑放在 `web_backend/app.py` 的 `run_agent_safely()` 中。接入时保持两个原则：

1. 不要把用户输入拼成 shell 命令。
2. 不要把 API key、服务器路径、错误堆栈返回给前端。
