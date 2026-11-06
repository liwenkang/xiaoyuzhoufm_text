# 声音留存 · 小宇宙平台文稿

粘贴小宇宙公开免费单集链接，读取平台已有的带时间戳文稿。**普通用户无需登录小宇宙，也无需获取、复制或填写 access token / refresh token。** 账号凭据由维护者在服务端统一管理，服务按需自动续期。

在线使用：<https://xiaoyuzhoufmtext.vercel.app>

前端为静态 HTML/CSS/JavaScript，后端为 Vercel 托管的 FastAPI Function。只读取已有文稿，不发起文稿生成，也不下载或识别音频。

## 使用（普通用户）

1. 打开上方在线地址。
2. 填写单集链接、完整分享文字或 24 位单集 ID。
3. 点击「获取节目文字」，等待读取完成。
4. 搜索文稿、点击时间戳复制，或下载 TXT、Markdown、SRT、VTT、JSON。

整个使用流程只需节目链接，不需要打开开发者工具或配置任何凭据。以下部署和凭据录入步骤仅供维护者使用。

导出文件名使用节目名称，不适合文件名的字符替换为下划线，最多保留 100 个字符。最近查看最多保存 8 期完整文稿，存于当前浏览器的 `localStorage`，不跨设备同步；空间不足时当前文稿仍可查看和下载。

平台未提供的结束时间由下一段开始时间或节目时长推算，JSON 和 Markdown 会标注。JSON 的 `language` 为 `und`、`model` 为 `null`，不推测平台未提供的信息。

### 遇到问题

- 服务繁忙或请求过多：稍后重新提交节目链接。
- 服务账号登录失效或凭据未配置：通过页面中的「联系维护者」访问 <https://www.liwenkang.space/about>，由维护者处理；普通用户无需自行登录或获取 token。
- 平台没有提供文稿：选择其他已有文稿的公开免费单集；本工具不会自动生成文稿。

## 自行部署与维护（仅维护者）

本节面向自行部署本项目或管理服务账号的维护者。直接使用在线服务的普通用户可以跳过；凭据首次录入、失效后的重新登录和更新均由维护者负责。

需要 Python 3.12+。建议使用专门的服务账号，避免在其他工具中同时刷新同一组凭据。

### 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m app.credentials
.\run.ps1
```

录入程序依次提示输入 access token 和 refresh token，输入不显示、不作为命令参数，不写入命令历史。access token 可留空，refresh token 必填。程序只保存凭据，不联网验证；首次读取节目时才会验证。请在本机交互终端执行，不要通过管道传递凭据。

未设置 `REDIS_URL` 时，本地将凭据、缓存和限流状态保存在 `.secrets/state.sqlite3`，可跨进程和重启使用。该目录被 Git 和 Vercel 部署排除，不会被静态资源路由提供。它包含明文凭据，仅应由维护者访问。

在系统默认浏览器手动打开 `http://127.0.0.1:8000`。本项目不自动化浏览器登录。

### 维护者手动取得初始凭据

1. 在系统默认浏览器打开 <https://accounts.xiaoyuzhoufm.com/login>。
2. 按 F12 打开开发者工具，进入「网络」，勾选「保留日志」。
3. 使用手机号和验证码登录，搜索 `login-with-sms` 请求。
4. 在该请求的响应头中找到 `x-jike-access-token` 和 `x-jike-refresh-token`，分别复制它们的值，录入本机命令行提示框。

不要复制头名称、引号或额外空格，不要把凭据发到聊天、放进前端代码、提交到 Git 或作为命令行参数。若登录响应未提供 refresh token，需要手动确认平台当前登录协议；只有 access token 无法启用本项目的自动续期。不要分享整个网络日志，其中可能含凭据。

### Vercel 部署

线上必须配置持久化 Redis。未配置 `REDIS_URL` 时，Vercel 请求返回 503，不会回退到临时文件系统。

1. 准备支持标准 Redis 协议、Lua/EVAL 和持久化的 Redis 实例（例如 Upstash 的 Redis TCP/TLS 连接）。配置保留数据和 `noeviction` 策略，避免共享凭据被缓存淘汰。Redis 数据丢失后必须重新录入账号。
2. 在 Vercel 项目环境变量中设置 `REDIS_URL`，使用完整的 `rediss://...` 连接地址。它不是 `UPSTASH_REDIS_REST_URL` 或 REST token。本项目不自动读取 `.env`，本地需要使用当前进程环境变量或下面的交互录入方式。
3. 在本机项目目录执行：

   ```powershell
   .\.venv\Scripts\python.exe -m app.credentials --redis
   ```

   依次在隐藏输入中录入线上 Redis URL、access token（可留空）、refresh token。轮换后的凭据会直接保存到该 Redis，之后无需修改 Vercel 环境变量。
4. 部署应用。环境变量的新增或修改需要重新部署；仅通过录入命令更新 Redis 中的账号凭据则无需重新部署。
5. 在页面提交一个有平台文稿的公开免费单集，验证真实账号鉴权和读取。

生产与预览环境应使用不同的 Redis 实例。每个实例的固定键前缀为 `xyztext:{v1}:`，不同账号或项目不要共用同一实例；克隆 Redis 备份到另一运行环境也会导致同一 refresh token 被并发刷新。

项目生产分支为 `main`。运行依赖在 `pyproject.toml`，入口为 `app.serverless:app`；`public/` 由 Vercel CDN 提供，本地由 FastAPI 挂载。Function 最大执行时长为 180 秒，页面等待时间为 185 秒，以容纳首次读取和续期；超时后可以稍后重试。

## 服务端续期、限流与缓存（维护者参考）

- 若 token 可解析出 `exp`，到期前 60 秒按需刷新；该字段仅用于调度，不用作身份验证。无法解析到期时间时，先尝试文稿请求，收到 401 后刷新并最多重试一次。没有请求时不启动后台定时任务。
- 续期固定请求 `https://api.xiaoyuzhoufm.com/app_auth_tokens.refresh`，refresh token 只放在 `x-jike-refresh-token` 请求头中；不跟随重定向，不读取或记录响应正文。
- 新 access token 与轮换后的 refresh token 在锁内一起持久化，保存成功后才使用新 access token。若平台只返回新的 refresh token，也先保存，避免丢失轮换结果。
- 凭据锁为 60 秒租约，锁释放和凭据写入均校验持有者。所有 Function 实例共享同一把锁；锁被占用时返回可重试的 503，不阻塞等待。
- 刷新接口明确返回 401/403 时标记「需要维护者重新登录」，停止自动刷新；网络错误、异常响应或刷新后仍被拒绝会冷却 60 秒。文稿接口的 403 不当作 token 过期，不触发刷新。
- 所有实例共享固定分钟窗口限流：总请求每分钟 30 次；未命中文稿缓存的请求每分钟 10 次。429 附 `Retry-After: 60`，限制不依赖可伪造的客户端 IP 请求头。
- 缓存未命中的上游任务全局串行，同一时刻最多一个请求读取新文稿。锁占用时返回 503 和 `Retry-After: 5`；Function 异常结束后的锁最多保留 240 秒。此保守策略适合小流量试用。
- 成功文稿按单集 ID 共享缓存 1 小时，仅缓存小于等于 1 MiB 的响应。失败不缓存，不保存文稿签名 URL；缓存中只含可以返回给页面的节目元数据和文稿。账号需要恢复期间，已缓存的文稿仍可读取。

默认限流参数位于 `app/shared_transcript.py`，并非平台公布的安全额度；需要根据真实使用情况调整。限流和缓存减少账号压力，不能保证平台不限制共享账号。

## 故障恢复和安全边界（维护者参考）

出现「服务账号登录已失效」时，维护者手动重新登录，然后重新运行 `python -m app.credentials`（本地）或加 `--redis`（线上）覆盖凭据；录入操作使用同一把凭据锁，也会清除恢复标记和冷却状态。普通使用者无需提交 token。

数据库连接失败返回脱敏的 503，程序不会继续使用未保存的轮换结果。但平台续期和本地保存不是跨系统事务：若平台已轮换而网络断开、进程中断或数据库写入失败，新凭据仍可能丢失，此时只能手动重新登录。

文稿与刷新接口依赖非公开协议。续期不能避免账号被限制、凭据撤销、验证码或平台协议变更，也无法保证永久可用。当前协议参考社区实现：<https://github.com/hesorchen/xiaoyuzhou-juicer/blob/main/scripts/xyz_fetch.py>；上线前仍需真实账号验证。

页面和 API 不提供读取、导出或在线修改凭据的管理接口。请求体只允许 `url`，旧的 `{url, token}` 请求被拒绝且不回显内容。后端凭据仅发送给固定小宇宙 API 域名，不发送到文稿 CDN；CDN 使用独立客户端，重定向与 DNS 校验继续保留。

`/api/health` 只报告进程可响应和共享账号模式，不验证凭据、数据库或上游平台是否可用。公开页面的“服务已连接”也只表示本站接口可达。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | `status: ok`、`transcript_mode: platform`、`auth_mode: server_shared` |
| POST | `/api/transcript` | 只接受 `{"url":"小宇宙单集链接或 ID"}` |
| GET | `/docs` | FastAPI 接口文档 |

成功响应包含 `episode`、`segments`、`transcript_source`、`source_note`、`end_timestamps_inferred`。每段包含秒数 `start` / `end`、`text` 和 `speaker`（当前为 `null`）。不会返回 token、音频地址或内部文稿标识。

错误响应包含 `detail`，业务错误另有 `error_code`。无效输入返回 400/422，文稿访问被拒绝返回 403，限流返回 429，共享凭据缺失、需要恢复、服务繁忙或存储故障返回 503。所有 API 响应设置 `Cache-Control: no-store`；服务端文稿缓存与浏览器 HTTP 缓存互相独立。

## 项目结构与验证

```text
app/serverless.py          API 和静态资源
app/shared_transcript.py   共享账号读取、限流和文稿缓存
app/credentials.py         自动续期、凭据轮换和维护者录入命令
app/state.py               SQLite / Redis、原子计数和带持有者校验的租约
app/platform_transcript.py 平台文稿协议、CDN 校验和时间戳解析
app/xiaoyuzhou.py          链接规范化和公开免费节目解析
public/                   前端 HTML、脚本和样式
tests/                    离线后端和前端测试
```

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
node --check public/static/app.js
npm ci --ignore-scripts
npm test
```

自动测试不使用真实凭据，也不访问小宇宙。Redis 测试使用 FakeRedis 和真实 Lua 执行器，覆盖轮换、刷新互斥、持有者校验、持久化失败、续期冷却、恢复、限流和缓存；它不能替代线上 Redis 连通性和真实协议验证。前端测试覆盖只提交链接、帮助、重复提交、错误恢复、搜索、复制、历史和导出。

遵守浏览器限制，不运行浏览器自动化；真实登录、凭据录入、页面视觉和线上联调需要在系统默认浏览器或本机交互终端手动完成。
