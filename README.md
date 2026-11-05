# 声音留存 · 小宇宙平台文稿

在线地址：<https://xiaoyuzhoufmtext.vercel.app>

粘贴小宇宙公开免费单集链接和自己的 access token，读取平台已经提供的带时间戳文稿。应用使用静态 HTML/CSS/JavaScript 和 Vercel 托管的 FastAPI Function，不需要维护服务器。

## 使用方式

1. 在页面填写小宇宙单集链接、完整分享文字或 24 位单集 ID。
2. 在 access token 输入框填写自己的 token。
3. 点击「获取节目文字」，等候本次请求完成。
4. 搜索文稿、点击时间戳复制，或下载 TXT、Markdown、SRT、VTT、JSON。

token 输入框支持显示/隐藏；点击「如何获取？」会展开操作说明。读取期间会显示状态并防止重复提交；文稿搜索会高亮关键词并显示匹配段落数。

下载按钮及文件名使用「节目名称 + 后缀」，例如 `EP9 有朋一起自驾，不亦乐乎？.txt`。不适合文件名的字符替换为下划线，名称最多保留 100 个字符。

最近查看最多保留 8 个节目的完整文稿，存于当前浏览器的 `localStorage`。这些记录不跨设备同步；清理网站数据会删除记录。浏览器存储空间不足时，当前文稿仍可查看和下载，但新记录可能无法保存。

## 获取 access token

页面内提供同样的操作说明，登录和复制均由使用者手动完成：

1. 在系统默认浏览器打开 [小宇宙账号登录页](https://accounts.xiaoyuzhoufm.com/login)。
2. 按 `F12` 打开开发者工具，进入「网络（Network）」，勾选「保留日志（Preserve log）」。
3. 使用手机号和验证码登录，在网络请求中搜索 `login-with-sms`。
4. 选择 `https://web-api.xiaoyuzhoufm.com/v1/auth/login-with-sms` 请求。
5. 打开「标头（Headers）」中的「响应头（Response Headers）」，找到 `x-jike-access-token`，只复制其值并粘贴到页面。

不要复制请求头名称或 refresh token。不要把 token 发给别人，token 失效后重新获取并替换即可。登录页面或请求结构变化时，需要按平台实际情况调整获取方式。

## token 和数据如何处理

- token 保留在当前页面的密码输入框中，可以在页面打开期间重复使用；应用不将其保存到浏览器存储。
- 提交时，浏览器通过 HTTPS 将 token 和节目链接发送到本站的 Function。token 会经过部署者的服务端，因此应只在信任的部署中输入。
- Function 在本次请求中使用 token，仅向固定的 `api.xiaoyuzhoufm.com` 文稿接口发送。文稿 CDN 请求使用独立客户端，不转发 token 或 API Cookie；API 请求不跟随重定向，CDN 每次跳转都校验地址。
- 应用不主动记录请求体或 token，不写入文件、数据库、环境变量、历史记录或导出文件；接口错误不会回显凭据或文稿签名地址。
- 每个用户必须提供自己的 token。没有部署者共享 token，也没有环境变量或本机凭据文件的回退逻辑。

## 当前架构和边界

```text
浏览器：public/index.html + public/static/app.js + style.css
  │ POST /api/transcript {url, token}
  ▼
Vercel FastAPI Function：app/serverless.py
  ├─ 读取公开节目页面，解析元数据和文稿标识
  ├─ 使用本次请求的 token 获取已有文稿地址
  ├─ 下载并校验文稿 JSON，转换毫秒时间戳
  └─ 返回节目和 segments
  ▼
浏览器：展示、搜索、localStorage 历史、Blob 文件导出
```

平台没有现成文稿时直接提示错误，不发起平台文稿生成请求。没有本地识别、音频下载/播放、服务端任务历史、排队或后台重试；失败后可重新提交。接口依赖小宇宙的非公开协议，平台变更可能导致读取失败。

平台未提供的段落结束时间由下一段开始时间或节目时长推算；JSON 的 `end_timestamps_inferred` 和 Markdown 会标注。JSON 的 `language` 为 `und`、`model` 为 `null`，不推测平台未提供的信息。

## Vercel 部署与更新

当前项目已关联 GitHub 的 `liwenkang/xiaoyuzhoufm_text` 仓库，生产分支为 `main`。

- `pyproject.toml` 声明运行依赖 FastAPI、HTTPX，并通过 `tool.vercel.entrypoint = "app.serverless:app"` 指定入口。
- `.python-version` 指定 Python 3.12，`vercel.json` 将 Function 最大执行时长设为 30 秒。
- `public/` 是唯一前端源目录。Vercel 直接通过 CDN 提供首页和 `public/static/` 下的脚本、样式，文件不进入 Function。本地开发时 FastAPI 将同一目录挂载到根路径，接口路由优先。
- `.vercelignore` 排除本地环境、缓存、测试和历史数据。
- 不需要配置任何 access token 环境变量；请求超过 Function 时限会失败，可稍后重试。

新部署：在 Vercel 导入仓库，使用仓库根目录，让其识别 FastAPI，构建与输出目录使用默认配置。

更新：提交并推送 `main` 后查看 Vercel 的自动部署状态；已在本机登录并关联项目时，也可通过 CLI 发布：

```powershell
npx --yes vercel@latest deploy --prod --yes
```

部署后检查首页、`/static/app.js`、`/static/style.css` 和 `/api/health`，并在页面使用自己的 token 验证节目文稿读取。

## 本地开发

需要 Python 3.12；运行前端测试还需要 Node.js。Python 依赖统一声明在 `pyproject.toml`，`dev` 附加依赖包含 Uvicorn、pytest 和 Ruff。前端交互测试的 DOM 替身 LinkeDOM 通过 `package.json` / `package-lock.json` 管理，仅用于开发，不进入 Vercel 部署或用户页面。

Windows 可直接执行：

```powershell
.\run.ps1
```

或手动安装和启动：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m uvicorn app.serverless:app --host 127.0.0.1 --port 8000
```

用系统默认浏览器手动打开 `http://127.0.0.1:8000`，在页面填写 token，无需配置环境变量。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 状态、平台文稿模式和 `auth_mode: user_supplied`；不检查上游是否可达 |
| POST | `/api/transcript` | 一次请求读取平台文稿 |
| GET | `/docs` | FastAPI 交互式接口文档 |

```http
POST /api/transcript
Content-Type: application/json

{"url":"https://www.xiaoyuzhoufm.com/episode/单集ID","token":"自己的 access token"}
```

成功响应包含 `episode`、`segments`、`transcript_source: xiaoyuzhou`、`source_note`、`end_timestamps_inferred`。每段包含秒数 `start` / `end`、`text` 和 `speaker`（当前为 `null`）。不会返回 token、音频地址或内部文稿标识。

错误响应包含 `detail`；业务错误另有 `error_code`。缺失/类型不符的参数返回 422，token 被拒绝返回 401，无访问权限返回 403，其他输入或文稿读取错误通常返回 400。API 响应设置 `Cache-Control: no-store`。

## 项目结构

```text
app/
  serverless.py          FastAPI 入口、请求校验、响应和静态资源
  platform_transcript.py 平台协议、临时 token、CDN 校验和时间戳解析
  xiaoyuzhou.py          链接规范化和节目公开页面解析
  errors.py             可返回给用户的业务错误
public/                 唯一 HTML/CSS/JavaScript 源目录
tests/                 离线接口、文稿解析和前端测试
pyproject.toml          运行/开发依赖、测试/lint 和 Vercel 入口
package.json           前端交互测试命令与开发依赖
DESIGN.md              页面视觉与交互约定
.python-version         Python 3.12
vercel.json             Function 时长配置
.vercelignore           部署排除项
run.ps1                 本地开发启动脚本
```

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
node --check public/static/app.js
npm ci --ignore-scripts
npm test
```

自动测试使用 HTTP 替身，不联网获取真实文稿，也不需要真实 token。覆盖链接解析、平台文稿时间戳、上游鉴权错误、token 与 CDN 隔离、重定向和私网阻断，以及当前无状态接口。

前端测试还覆盖 token 显示与帮助、重复提交、加载恢复、网络和剪贴板错误、搜索高亮、历史数据容错及导出文件名。DOM 测试不替代真实浏览器中的视觉和键盘检查。
