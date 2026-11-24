# Auto Linux.do

Linux.do 和 V2EX 只读 MCP 服务。AI 客户端可以获取主题和帖子。所有论坛访问均通过 Docker 内的 Google Chrome。服务以非 root 用户运行，并从 `chrome://sandbox` 验证命名空间和 Seccomp-BPF 沙箱。

## 启动和接入

需要 x86_64 Linux、Docker Engine、Docker Compose，以及允许非特权用户命名空间的宿主机。

```bash
docker compose up -d --build --wait
docker compose exec -T linuxdo python -m linuxdo.admin token
```

将 AI 客户端的传输方式设为 **Streamable HTTP**，地址填 `http://127.0.0.1:8765/mcp`，请求头填 `Authorization: Bearer <token 命令输出>`。这个密钥供 AI 客户端读取论坛内容。

## 从 Chrome 同步

在自己的 Chrome 登录 Linux.do，解压服务器生成的专用扩展 ZIP，再打开 `chrome://extensions`，开启开发者模式并加载解压后的 `extension` 文件夹。打开「Linux.do 会话同步」并点击「同步」。首次点击时 Chrome 会请求 Linux.do 权限。启用后，扩展会在 Cookie 变化时同步，并每分钟检查一次。浏览器退出论坛后，服务器会清除由这版扩展同步的会话。

扩展内置 HTTPS 地址和专用连接码，只同步 Linux.do 主域的 `_t`。服务器通过专用端口对外提供 Cookie 同步入口，MCP 使用本机端口。ZIP 含连接码，请仅通过私有渠道交付。

### 在服务器上部署并生成专用扩展

1. 将 [Nginx 配置示例](deploy/nginx-sync-acme.conf.example)中的 IP 改为服务器公网 IPv4，安装到现有 Nginx 的 `conf.d`，运行 `nginx -t` 并重载。该配置将该公网 IP 上的 HTTP-01 证书验证转给 Caddy。
2. 用下列命令部署 Caddy 和服务，然后生成解压即用的扩展包。把示例 IP 换成实际公网 IPv4，公开 TCP 端口 `80` 和 `8443`，并保留 Docker 的 `sync_tls` 卷供 Caddy 自动续签。

```bash
export LINUXDO_SYNC_IP=152.53.126.151
docker compose -f compose.yaml -f compose.remote-sync.yaml up -d --build --wait
umask 077
docker compose exec -T linuxdo python -m linuxdo.package_extension "$LINUXDO_SYNC_IP" > linuxdo-sync-private.zip
```

证书取得后，可以用 `curl -i "https://$LINUXDO_SYNC_IP:8443/cookie-sync"` 检查 HTTPS。未认证的 GET 应返回 404。只有通过认证的扩展 POST 或 DELETE 请求可以更新会话。

### 手动导入

停用扩展后，可以通过终端粘贴当前账号的 Cookie 请求头。也可以从标准输入导入请求头、Cookie JSON 数组或包含 `cookies` 数组的 JSON 文件，文件格式可使用 Playwright storage state。

```bash
docker compose exec linuxdo python -m linuxdo.admin configure
docker compose exec -T linuxdo python -m linuxdo.admin configure < linuxdo-cookies.json
```

导入需要 Linux.do 的 `_t`，可同时导入 `_forum_session`。导入器会忽略其他来源和 Cloudflare 验证 Cookie。手动导入完整 JSON 可以保留 Cookie 到期时间。请求头不包含到期时间，因此按 Chrome 会话 Cookie 导入。

## AI 工具

| MCP 工具 | 参数与结果 |
| --- | --- |
| `forum_status` | 返回沙箱和账号状态。调用时不向论坛发请求 |
| `forum_latest` | `limit` 默认为 10，最多 25。返回主题 ID、标题和原文链接 |
| `forum_search` | `query` 最多 160 字符，`limit` 默认为 10，最多 25 |
| `forum_read_topic` | 指定 `topic_id`，可传 `post_number` 定位楼层。返回从该楼层起当前页面已加载的帖子，`limit` 默认为 5，最多 10 |

## V2EX

V2EX 的账号密码登录页要求图形验证码。公开网页读取无需配置账号。需要使用官方 API 2.0 时，先在自己的浏览器登录，在令牌设置中生成个人访问令牌（PAT），再在 Docker 终端配置。令牌有效期最长为 180 天，到期后可运行同一命令替换。 [V2EX 登录页](https://www.v2ex.com/signin)、[令牌设置](https://www.v2ex.com/settings/tokens)、[官方令牌说明](https://www.v2ex.com/help/personal-access-token)、[API 文档](https://www.v2ex.com/help/api)

```bash
docker compose exec linuxdo python -m linuxdo.admin v2ex-configure
```

| MCP 工具 | 参数与结果 |
| --- | --- |
| `v2ex_status` | 查看沙箱、访问暂停和令牌配置状态 |
| `v2ex_latest` | 读取公开最新主题，支持 `page` 和 `limit` |
| `v2ex_node_topics` | 按节点读取主题，`node` 为短名，例如 `python`，支持 `page` 和 `limit` |
| `v2ex_read_topic` | 用 `topic_id` 读取正文 |
| `v2ex_replies` | 用 `topic_id`、`page` 和 `limit` 按页读取回复 |

配置令牌后，节点、正文和回复使用官方 API 2.0，仍经过 Chrome 沙箱。最新主题来自公开网页。令牌保存在 Docker 数据卷中，可用 `docker compose exec linuxdo python -m linuxdo.admin v2ex-forget` 清除。

遇到人工验证、HTTP 403 或限流时，V2EX 读取会暂停，Linux.do 可以独立使用。限流遵守 `Retry-After`。站点限制解除后，可运行 `docker compose exec linuxdo python -m linuxdo.admin v2ex-resume` 恢复。

## 会话与运行状态

- Chrome 会保存论坛通过正常浏览更新的 Cookie。服务默认每 7200 秒检查会话，读取时也会检查。可用 `LINUXDO_REFRESH_SECONDS` 调整间隔，最短 900 秒。会话被论坛撤销或到期时，需在自己的 Chrome 重新登录，扩展随后会同步新 Cookie，也可手动重新导入。
- 导入的 Cookie 和 Chrome 配置保存在 Docker 命名卷中，Cookie 配置文件和密钥文件权限为 `0600`。升级时服务会删除旧账号密码文件。清除服务会话可运行 `docker compose exec linuxdo python -m linuxdo.admin forget`。
- 论坛发出验证码、二次验证、访问拒绝或限流时，服务暂停并记录原因。重新同步 Cookie 不会解除站点暂停。限流遵守 `Retry-After`。站点限制解除后，运行 `docker compose exec linuxdo python -m linuxdo.admin resume` 恢复检查。
- MCP 默认只监听宿主机回环地址，端口可用 `LINUXDO_PORT` 修改。健康检查使用 `/healthz`。需要更新 Chrome 和系统包时，用 `docker compose build --pull --no-cache` 重建，再用 `docker compose up -d --wait` 更新服务。

## Docker 验证

下列测试在隔离网络中的本地模拟论坛上运行。它们覆盖 Chrome 沙箱、Linux.do 会话和内容读取、V2EX 公开网页、令牌 API 读取、凭据范围和站点限制。

```bash
docker build --target test -t auto-linuxdo:test .
docker run --rm --init --network none --read-only --tmpfs /tmp:mode=1777 \
  --shm-size 256m --cap-drop ALL --security-opt no-new-privileges:true \
  --security-opt seccomp=deploy/chrome-seccomp.json \
  -e XDG_CONFIG_HOME=/tmp/chrome-config -e XDG_CACHE_HOME=/tmp/chrome-cache \
  auto-linuxdo:test
```

## 实站兼容性

Pi 检查了公开页面和 Discourse 上游实现。当前部署环境访问 Linux.do 时触发了人工验证，真实账号 Cookie 同步后的论坛读取仍待验收。已核对的上游依据包括 [会话 Cookie](https://github.com/discourse/discourse/blob/main/lib/auth/default_current_user_provider.rb)、[搜索页面](https://github.com/discourse/discourse/blob/e077161ff34dca5e53cacb4e16430516143d3c67/frontend/discourse/app/templates/full-page-search.gjs)和[主题模板](https://github.com/discourse/discourse/blob/e077161ff34dca5e53cacb4e16430516143d3c67/frontend/discourse/app/templates/topic.gjs)。

当前部署环境访问 V2EX 公开最新主题时也触发了人工验证。V2EX 实站取数和真实令牌均待验收。

Seccomp 配置取自 [Playwright v1.64.1](https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json)，增加了 Chrome 在自身用户命名空间中建立沙箱所需的 `chroot` 系统调用。容器丢弃全部初始 Linux capabilities。
