# gcmw 当前系统基线与差距盘点

- 盘点日期：2026-09-05
- 基线提交：`d9951ded4e6f6b62b70dda8b0d4ddffb96aa06b8`
- 对应 Issue：#2
- 对应计划：gcmw V2.3 第 3 章

## 1. 可复用能力

### 1.1 Electron/Vue 前端

- Electron + Vue 3 + TypeScript + Tailwind。
- 页面：
  - `/` 首页导航
  - `/qa` 互动问答
  - `/science` 视光科普
  - `/about` 特色技术
  - `/member` 人员介绍
- 现有组件：
  - 问答页“小视”点阵表情
  - 浏览器 TTS
  - Markdown 渲染
  - LiquidGlass 等 UI 组件
- 资源协议：`rc://` 读取外部 YAML/Markdown/图片/视频。

### 1.2 FastAPI 后端

- 入口：`botscreen-public/server/qa_server.py`
- 当前路由：
  1. `POST /chat`
  2. `GET /suggestions`
  3. `POST /mic/wakeup`
  4. `POST /mic/hw_wakeup`
  5. `POST /mic/stop`
  6. `GET /mic/status`
  7. `POST /mic/notify_asr`
  8. `GET /sse`
  9. `GET /health`
- 技术：FastAPI + Uvicorn + jieba + requests。
- 现有能力：DOCX/内置 FAQ 匹配、DeepSeek 兜底、SSE、麦克风状态。

### 1.3 ROS 语音链路

- ROS1：`voice_transfer_node.py`
- ROS2：`voice_transfer_node_ros2.py`
- 启动脚本：`start_robot.sh` / `start_robot_ros2.sh`
- 已具备：唤醒、ASR 回传、问答请求、TTS 状态衔接。

### 1.4 内容数据

- `knowledge_base.yml`：9 条儿童眼健康 FAQ
- `qa_server.py`：内置 FAQ 兜底
- `departments/departments.yml`：8 个特色技术
- `departments/*.md`：8 篇详情/科普
- `members/members.yml`：17 位人员
- `science-video/index.yml`：36 条视频元数据

## 2. 关键差距

| 领域 | 现状 | 目标差距 |
|---|---|---|
| Agent | 无 Manager/子 Agent/Verifier | 需要 Manager + MedicalQA + Verifier |
| 会话隔离 | 全局 `_latest_answer`、共享 SSE、默认 `default` 会话 | 必须 Run/Device/Session 级隔离 |
| SSE | 全局广播 | 双层、可重放、鉴权、按 Run 隔离 |
| 知识治理 | 内容未进入受控审核/版本体系 | 候选区/生产区、来源、版本、审核、撤回 |
| 模型接入 | DeepSeek 同步 HTTP | ModelGateway + Cloud/Local/Mock Provider |
| 多模态 | 目前仅文字/语音链路 | text/audio/image，video 预留 |
| 安全 | CORS 全开、无设备认证、日志未脱敏、硬编码口令 | 生产级认证/限流/脱敏/Secret 管理 |
| 可观测 | 无 trace/审计/metrics | 全链路 trace、审计、指标、告警 |

## 3. 多模态缺口

- 当前 Electron/Vue 页面仅支持文字输入和语音唤醒。
- 没有图片上传、图片 OCR 或图文联合问答。
- 后端没有 `content_parts` 或图片输入模型。
- ROS 语音链路未做音频文件/流式识别标准。
- 没有视频理解或视频时间码引用。

## 4. 已知运行风险/失败证据

- SSE 使用全局最新回答，多会话时可能串话。
- ROS 请求没有独立 `session_id`，默认进入共享会话。
- `/sse` 无鉴权，任何能访问 8000 端口的人可订阅事件。
- `qa_server.py` 读取外部 DOCX，若文件不存在则使用内置知识，但内置知识与 YAML 存在双源。
- Electron 主进程存在硬编码退出口令。
- 仓库中可能存在疑似 AIUI 凭据，需 Secret 扫描确认。
- 日志中可能打印问题和回答明文，未脱敏。
- 未实现取消、超时、幂等、审计和引用。

## 4.1 关键依赖版本

| 依赖 | 版本来源 | 版本 |
|---|---|---|
| Electron | package.json | ^40.0.0 |
| Vue | package.json | ^3.5.27 |
| TypeScript | package.json | ^5.9.3 |
| FastAPI | server/requirements.txt | >=0.100.0 |
| Uvicorn | server/requirements.txt | >=0.23.0 |
| Pydantic | 本机核验 | 2.13.5（正式基线需在 3.11/3.12 锁定） |
| jieba | server/requirements.txt | >=0.42.1 |
| Python | 开发机 3.14.6；正式基线 3.11/3.12 | 3.14.6 / 3.11 / 3.12 |

## 4.2 复核记录

- 复核日期：2026-09-05
- 复核人：Snow7
- 复核方式：仓库静态盘点 + 接口 grep
- 本机已提供关键版本实际输出；精确锁定版本以 CI/正式环境为准。

## 4.3 实际环境证据（2026-09-05 本机核验）

```text
$ python3 --version
Python 3.14.6

$ node --version
v24.18.0

$ npm --version
11.17.0

$ git --version
git version 2.50.1 (Apple Git-155)

$ python3 -c "import fastapi,pydantic,pytest; print(fastapi.__version__, pydantic.__version__, pytest.__version__)"
0.141.1 2.13.5 9.1.1
```

失败日志：本次核验未触发运行失败；CI 运行失败日志待 GitHub Actions 输出后补充。

## 5. 复核命令

```bash
# 查看提交
git rev-parse HEAD

# 查看后端路由
grep -n "@app\.\(get\|post\)" botscreen-public/server/qa_server.py

# 查看页面
find botscreen-public/src/renderer/src/pages -maxdepth 3 -type f

# 查看 ROS 节点
sed -n '1,220p' botscreen-public/server/voice_transfer_node.py
sed -n '1,240p' botscreen-public/server/voice_transfer_node_ros2.py

# 查看数据源
wc -l botscreen-public/server/knowledge_base.yml departments/departments.yml members/members.yml science-video/index.yml
```

## 6. 结论

当前基线适合作为“保留 UI/ROS/内容资源、逐步抽离 Agent 后端”的起点；在接入真实用户前必须先完成会话隔离、安全底座、知识治理和 ModelGateway。
