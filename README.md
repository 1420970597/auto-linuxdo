# Auto Linux.do

Linux.do 和 V2EX 只读 MCP 服务。AI 客户端可以获取主题和帖子。所有论坛访问均通过 Docker 内的 Google Chrome。服务以非 root 用户运行，并从 `chrome://sandbox` 验证命名空间和 Seccomp-BPF 沙箱。

## 启动和接入

需要 x86_64 Linux、Docker Engine、Docker Compose，以及允许非特权用户命名空间的宿主机。

```bash
docker compose up -d --build --wait
docker compose exec -T linuxdo python -m linuxdo.admin token
```

将 AI 客户端的传输方式设为 **Streamable HTTP**，地址填 `http://127.0.0.1:8765/mcp`，请求头填 `Authorization: Bearer <token 命令输出>`。这个密钥供 AI 客户端读取论坛内容。

## 从 Chrome 自动同步

1. 在自己的 Chrome 登录 Linux.do。打开 `chrome://extensions`，开启开发者模式，选择「加载已解压的扩展程序」，选择项目中的 `extension` 文件夹。
2. 在 Docker 所在机器运行 `docker compose exec -T linuxdo python -m linuxdo.admin sync-token`。打开 Chrome 工具栏的「Linux.do 会话同步」，粘贴独立同步密钥，确认端口，点击「开启自动同步」，再授予 Linux.do 访问权限。
3. 扩展会在 Cookie 更新时同步，每分钟也会检查一次。源浏览器退出论坛后，下一次检查会清除由扩展配置的本机会话。扩展面板和 MCP 的 `forum_status` 可查看同步与登录状态。

扩展只读取 Linux.do 主域的 `_t` 会话 Cookie。它先用同步密钥验证回环地址上的目标服务，再发送 Cookie。配对信息保存在当前 Chrome 扩展的本地存储，Cookie 留在 Chrome 原有存储中。同步密钥与 MCP 读取密钥分开使用。

浏览器和 Docker 在不同机器时，可运行 `ssh -N -L 8765:127.0.0.1:8765 <Docker 主机>`，并让扩展继续连接浏览器机器的回环地址。

### 手动导入

暂停自动同步后，可以通过终端粘贴当前账号的 Cookie 请求头。也可以从标准输入导入请求头、Cookie JSON 数组或包含 `cookies` 数组的 JSON 文件，文件格式可使用 Playwright storage state。

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
- 导入的 Cookie 和 Chrome 配置保存在 Docker 命名卷中，Cookie 配置文件和两个密钥文件权限为 `0600`。升级时服务会删除旧账号密码文件。暂停扩展会解除配对。清除服务会话可运行 `docker compose exec linuxdo python -m linuxdo.admin forget`。
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
