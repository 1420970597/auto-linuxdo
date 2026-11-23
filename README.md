# Auto Linux.do

单账号 Linux.do 只读 MCP 服务。AI 客户端可以获取最新主题、搜索论坛和读取帖子。所有论坛页面都由 Docker 中的 Google Chrome 渲染。服务以非 root 用户运行，并且在启动时从 `chrome://sandbox` 验证命名空间和 Seccomp-BPF 沙箱。

## 启动和接入

需要 x86_64 Linux、Docker Engine、Docker Compose，以及允许非特权用户命名空间的宿主机。

```bash
docker compose up -d --build --wait
docker compose exec linuxdo python -m linuxdo.admin configure
docker compose exec -T linuxdo python -m linuxdo.admin token
```

`configure` 在终端提示输入 Linux.do 用户名或邮箱和密码。服务会在数秒内检查会话。将 MCP 客户端的传输方式设为 **Streamable HTTP**，地址填 `http://127.0.0.1:8765/mcp`，请求头填 `Authorization: Bearer <token 命令输出>`。这个 Token 是 MCP 访问密钥，持有它的客户端可读取当前论坛账号可访问的内容。

| MCP 工具 | 参数与结果 |
| --- | --- |
| `forum_status` | 返回沙箱和账号状态。调用时不向论坛发请求 |
| `forum_latest` | `limit` 默认为 10，最多 25。返回主题 ID、标题和原文链接 |
| `forum_search` | `query` 最多 160 字符，`limit` 默认为 10，最多 25 |
| `forum_read_topic` | 指定 `topic_id`，可传 `post_number` 定位楼层。返回从该楼层起当前页面已加载的帖子，`limit` 默认为 5，最多 10 |

## 会话与运行状态

- Chrome 在正常网页登录过程中接收和保存论坛会话。服务默认每 7200 秒检查会话，读取时也会检查。确认掉线时自动尝试一次网页登录。可用 `LINUXDO_REFRESH_SECONDS` 调整检查间隔，最短 900 秒。
- Chrome 配置、论坛密码和 MCP 密钥保存在 Docker 命名卷中。凭据和 MCP 密钥文件权限为 `0600`。论坛 Cookie 和密码仅由服务本地使用。再次运行 `configure` 会换用新 Chrome 配置，运行 `docker compose exec linuxdo python -m linuxdo.admin forget` 会清除账号及其本地会话。
- 论坛发出验证码、二次验证、访问拒绝或限流时，服务暂停并记录原因。限流遵守 `Retry-After`。处理完站点要求后，可用 `docker compose exec linuxdo python -m linuxdo.admin resume` 恢复检查。密码错误时重新运行 `configure`。
- MCP 默认只监听宿主机回环地址，端口可用 `LINUXDO_PORT` 修改。健康检查使用 `/healthz`。需要更新 Chrome 和系统包时，用 `docker compose build --pull --no-cache` 重建，再用 `docker compose up -d --wait` 更新服务。

## Docker 验证

下列测试在隔离网络中的本地模拟论坛上运行。它们覆盖真实 Chrome 沙箱、首次登录、重启复用、会话过期后重新登录、内容读取、错误密码、二次验证和限流。

```bash
docker build --target test -t auto-linuxdo:test .
docker run --rm --init --network none --read-only --tmpfs /tmp:mode=1777 \
  --shm-size 256m --cap-drop ALL --security-opt no-new-privileges:true \
  --security-opt seccomp=deploy/chrome-seccomp.json \
  -e XDG_CONFIG_HOME=/tmp/chrome-config -e XDG_CACHE_HOME=/tmp/chrome-cache \
  auto-linuxdo:test
```

## 实站兼容性

Pi 检查了公开页面和 Discourse 上游实现。当前部署环境访问 Linux.do 时触发了人工验证，真实账号登录和续期仍待验收。已核对的上游依据包括 [登录表单](https://github.com/discourse/discourse/blob/e077161ff34dca5e53cacb4e16430516143d3c67/frontend/discourse/app/components/local-login-form.gjs)、[搜索页面](https://github.com/discourse/discourse/blob/e077161ff34dca5e53cacb4e16430516143d3c67/frontend/discourse/app/templates/full-page-search.gjs)和[主题模板](https://github.com/discourse/discourse/blob/e077161ff34dca5e53cacb4e16430516143d3c67/frontend/discourse/app/templates/topic.gjs)。

Seccomp 配置取自 [Playwright v1.64.1](https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json)，增加了 Chrome 在自身用户命名空间中建立沙箱所需的 `chroot` 系统调用。容器丢弃全部初始 Linux capabilities。
