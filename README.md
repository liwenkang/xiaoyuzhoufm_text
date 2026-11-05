# 声音留存 · 小宇宙平台文稿

这是一个面向 Vercel 的无状态版本：粘贴小宇宙公开单集链接，服务端读取小宇宙已经生成的平台文稿，浏览器负责搜索、历史记录和导出文件。

## 当前能力

- 读取小宇宙公开免费单集的标题、播客、封面、时长和平台文稿。
- 显示带时间戳的文稿，并支持搜索和复制时间戳。
- 在浏览器中生成 TXT、Markdown、SRT、VTT、JSON 下载文件。
- 最近查看记录保存在当前浏览器的 `localStorage`；access token 不保存。
- 不下载原始音频，不做本地 Whisper 识别，不使用 SQLite、队列、常驻 worker 或服务器文件存储。

## 获取 access token

在系统默认浏览器打开小宇宙账号登录页，按 `F12` 打开开发者工具，进入「网络（Network）」并勾选「保留日志（Preserve log）」。使用手机号和验证码登录后，搜索 `login-with-sms`，选择 `https://web-api.xiaoyuzhoufm.com/v1/auth/login-with-sms` 请求。在「响应头（Response Headers）」中找到 `x-jike-access-token`，只复制它的值，粘贴到页面输入框。

页面会明确提示这一流程。不要复制请求头名称，不要复制 refresh token，也不要把 token 保存到公开代码或聊天记录中。

## Vercel 部署

1. 将仓库导入 Vercel。
2. 不需要设置 `XIAOYUZHOU_ACCESS_TOKEN` 环境变量。
3. 打开部署后的页面，由使用者在页面输入自己的 access token。

token 只随当前请求发送给 Function，不写入环境变量、文件、数据库或 `localStorage`，也不会返回到页面。部署者不会看到其他用户填写的 token；用户也不应把 token 发给别人或提交到 Git。

## 本地运行

本地开发只需要 Python 3.12+：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.serverless:app --host 127.0.0.1 --port 8000
```

然后用系统默认浏览器访问 `http://127.0.0.1:8000`，在页面填写 access token。

## 项目结构

```text
app/serverless.py         无状态 FastAPI 应用
app/platform_transcript.py 平台文稿读取和时间戳解析
app/xiaoyuzhou.py         小宇宙页面解析和链接校验
public/                   Vercel 静态页面、脚本和样式
vercel.json               Function 时长配置；`pyproject.toml` 指定 FastAPI 入口
```

旧的 `app/main.py`、`app/worker.py`、`app/db.py`、`app/transcriber.py` 等文件仍保留在仓库中，便于对照历史本地版本；Vercel 入口不会导入它们，也不会安装本地识别依赖。

## API

```http
POST /api/transcript
Content-Type: application/json

{"url":"https://www.xiaoyuzhoufm.com/episode/单集ID","token":"你的 access token"}
```

返回节目元数据、`segments`、`transcript_source` 和 `end_timestamps_inferred`。文稿地址和 access token 不会返回给前端。

平台文稿读取使用小宇宙当前可用的非公开接口协议，平台接口变化时可能需要更新 `app/platform_transcript.py`。只读取自己账号有权访问的节目。

## 验证

```powershell
.\.venv\Scripts\python.exe -m compileall app
node --check public/app.js
```
