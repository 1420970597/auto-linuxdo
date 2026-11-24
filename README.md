# Auto Linux.do

Linux.do 和 V2EX 只读 MCP 服务。AI 客户端可以获取主题和帖子。论坛访问由 [cfsolve](https://github.com/chris124567/cfsolve) 驱动的 `forum-fetch` 组件完成：它模拟 Safari 指纹，必要时自动通过 Cloudflare 人机验证，并复用 `cf_clearance` Cookie。服务以非 root 用户运行。

## 启动和接入

需要 x86_64 Linux、Docker Engine 和 Docker Compose。

```bash
docker compose up -d --build --wait
docker compose exec -T linuxdo python -m linuxdo.admin token
```

将 AI 客户端的传输方式设为 **Streamable HTTP**，地址填 `http://127.0.0.1:8765/mcp`，请求头填 `Authorization: Bearer <token 命令输出>`。这个密钥供 AI 客户端读取论坛内容。

## 传输组件

`forum-fetch` 是一个独立的 Rust 程序，和 Python 服务一起构建进镜像：

- 每次请求启动一个进程，读取浏览器指纹档案 `safari-26-ios.json`，用同样的 TLS/HTTP2 指纹访问论坛。
- 页面返回 Cloudflare 验证页时，调用 cfsolve 求解，拿到 `cf_clearance` 后重试一次。
- Cookie 保存在数据卷的 `cf-cookies.json` 中，代次之间复用；同步的 Linux.do 会话 Cookie 优先于旧值。
- 需要的浏览器指纹档案和录制的应答文件位于 `/usr/local/share/cfsolve/`，通过 `LINUXDO_FETCH_PROFILE` 可替换。
- 可用 `LINUXDO_FETCH_PROXY` 指定代理（`http://`、`https://`、`socks5://`、`socks5h://`）。

`forum_status` 的 `transport` 字段表示组件是否就绪。

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
| `forum_status` | 返回传输组件和账号状态。调用时不向论坛发请求 |
| `forum_latest` | `limit` 默认为 10，最多 25。返回主题 ID、标题和原文链接 |
| `forum_search` | `query` 最多 160 字符，`limit` 默认为 10，最多 25。搜索需要登录会话 |
| `forum_read_topic` | 指定 `topic_id`，可传 `post_number` 定位楼层。返回从该楼层起当前页面已加载的帖子，`limit` 默认为 5，最多 10 |

## V2EX

V2EX 的账号密码登录页要求图形验证码。公开网页读取无需配置账号。需要使用官方 API 2.0 时，先在自己的浏览器登录，在令牌设置中生成个人访问令牌（PAT），再在 Docker 终端配置。令牌有效期最长为 180 天，到期后可运行同一命令替换。 [V2EX 登录页](https://www.v2ex.com/signin)、[令牌设置](https://www.v2ex.com/settings/tokens)、[官方令牌说明](https://www.v2ex.com/help/personal-access-token)、[API 文档](https://www.v2ex.com/help/api)

```bash
docker compose exec linuxdo python -m linuxdo.admin v2ex-configure
```

| MCP 工具 | 参数与结果 |
| --- | --- |
| `v2ex_status` | 查看访问暂停和令牌配置状态 |
| `v2ex_latest` | 读取公开最新主题，支持 `page` 和 `limit` |
| `v2ex_node_topics` | 按节点读取主题，`node` 为短名，例如 `python`，支持 `page` 和 `limit` |
| `v2ex_read_topic` | 用 `topic_id` 读取正文 |
| `v2ex_replies` | 用 `topic_id`、`page` 和 `limit` 按页读取回复 |

配置令牌后，节点、正文和回复使用官方 API 2.0；未配置时读取公开网页。最新主题始终来自公开网页。令牌保存在数据卷中，可用 `docker compose exec linuxdo python -m linuxdo.admin v2ex-forget` 清除。

遇到人工验证、HTTP 403 或限流时，V2EX 读取会暂停，Linux.do 可以独立使用。限流遵守 `Retry-After`。站点限制解除后，可运行 `docker compose exec linuxdo python -m linuxdo.admin v2ex-resume` 恢复。

## 会话与运行状态

- 服务默认每 7200 秒检查会话，读取时也会检查。可用 `LINUXDO_REFRESH_SECONDS` 调整间隔，最短 900 秒。会话被论坛撤销或到期时，需在自己的 Chrome 重新登录，扩展随后会同步新 Cookie，也可手动重新导入。
- 导入的 Cookie 和 Cloudflare 验证 Cookie 保存在数据卷中，权限为 `0600`。升级时服务会删除旧账号密码文件。清除服务会话可运行 `docker compose exec linuxdo python -m linuxdo.admin forget`。
- Linux.do 返回验证码时 `forum-fetch` 会当场求解；求解失败按访问拒绝暂停（`site_blocked`），并记录原因。V2EX 的人工验证、访问拒绝或限流会暂停 V2EX 读取。重新同步 Cookie 不会解除站点暂停。限流遵守 `Retry-After`。站点限制解除后，运行 `docker compose exec linuxdo python -m linuxdo.admin resume` 恢复检查。
- MCP 默认只监听宿主机回环地址，端口可用 `LINUXDO_PORT` 修改。健康检查使用 `/healthz`，其中会检查 `forum-fetch` 及其指纹档案是否存在。

## 测试

单元测试不需要网络，也不需要构建 Rust 组件：

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

镜像内测试使用同一个入口：

```bash
docker build --target test -t auto-linuxdo:test .
docker run --rm --init --network none --read-only --tmpfs /tmp:mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges:true auto-linuxdo:test
```

覆盖范围：Linux.do JSON 解析与错误分类、V2EX 公开网页与令牌 API 读取、Cookie 同步接口和凭据范围。

## 组件来源

`forum-fetch` 依赖 [cfsolve](https://github.com/chris124567/cfsolve)，以 git submodule 形式放在 `vendor/cfsolve`。它带有打过补丁的 BoringSSL 和 HTTP/2 依赖，`Cargo.toml` 在 workspace 根声明了对应的 `[patch.crates-io]`。

克隆后需要初始化子模块：

```bash
git submodule update --init --recursive
```

单独构建传输组件（需要 Rust 1.98、CMake、C/C++ 编译器、libclang 和 Git）：

```bash
cargo build --release --bin forum-fetch
```

本地手动运行：

```bash
./target/release/forum-fetch --profile vendor/cfsolve/profiles/safari-26-ios.json \
  --url 'https://linux.do/latest.json' --cookie-file /tmp/jar.json
```
