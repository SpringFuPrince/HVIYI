# HVIYI 本地会议办公助手

这是单用户、本地优先的会议 Agent 项目。会议是 Graph、文档、任务和四层 Memory 的唯一业务边界；不含登录、组织、租户或用户管理。

## 页面与后端契约

- 首页：读取最近会议与未完成任务。
- 发起会议：创建 `meeting_id`，随后所有资料、聊天和记忆均归属该会议。
- 会议工作台：上传资料至 Import Graph；录音通过 WebSocket 推送真实音频，结束时生成 Markdown 并自动导入。
- 会议记录：读取本地会议列表。
- RAG 问答：必须选择一场会议，沿用 Query Graph 的 SSE 输出。

## 本地启动

在项目根目录启动统一后端服务：

```powershell
uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

再启动前端：

```powershell
cd front
npm.cmd run dev
```

Vite 开发代理如下：

- `/api/import` → `127.0.0.1:8000/api/import`
- `/api/query` → `127.0.0.1:8000/api/query`
- `/api/local` → `127.0.0.1:8000/api/local`（含 WebSocket）

前端生产构建：

```powershell
npm.cmd run build
```

## 实时 ASR

浏览器通过 AudioWorklet 连续采集麦克风，将音频转换为 16kHz、单声道、PCM16，每约 100ms 通过 WebSocket 发送一帧。后端持续把帧送入 FunASR 流式模型，同一连接中的模型 `cache` 会跨帧保留；结束时前端发送 `finish`，收到 `finished` 后才结束会议。

本地流式识别配置：

```env
ASR_PROVIDER=funasr_streaming
ASR_MODEL=paraformer-zh-streaming
ASR_DEVICE=cpu
ASR_CHUNK_SIZE=5,10,5
```

这条链路不调用云端 `audio.transcriptions.create`，模型直接运行在本机。没有返回真实 ASR 文本时，会议仍可结束，但不会生成转录 Markdown 或触发其 Import Graph。

## 数据隔离

MySQL 本地版表均以 `local_` 为前缀，避免修改旧多租户表。首次需要手工建表时运行：

```powershell
uv run python -m scripts.create_local_schema
```

Milvus 与 MongoDB 同样使用新的本地集合名；首次导入资料或首次查询时由对应服务初始化。
