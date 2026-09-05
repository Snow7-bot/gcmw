# Secret 清理与轮换记录

- 日期：2026-09-05
- 对应 Issue：#4
- 处理策略：方案 A（不重写 Git 历史；删除当前明文 + 外部轮换）

## 1. 发现与处理

| 位置 | 原值类型 | 处理 |
|---|---|---|
| `botscreen-public/src/main/index.ts` | Electron 退出口令 `114514` | 改为读取 `GCMW_EXIT_PASSWORD` 环境变量 |
| ROS1 `AIUI/cfg/aiui.cfg` | appid / key / api_secret | 替换为占位符 |
| ROS2 `AIUI/cfg/aiui.cfg` | appid / key / api_secret | 替换为占位符 |
| ROS1 `AIUI/assets/vtn/vtn.ini` | appid | 替换为占位符 |
| ROS2 `AIUI/assets/vtn/vtn.ini` | appid | 替换为占位符 |

## 2. 需要外部轮换

以下值已从当前代码/配置中移除，但可能仍存在于 Git 历史中：

- Electron 退出口令
- AIUI `appid`
- AIUI `key`
- AIUI `api_secret`
- VTN `appid`

由于采用方案 A，**不重写 Git 历史**，因此必须确认这些凭据已经在对应平台完成轮换/失效。

## 3. 后续接入方式

- Electron 退出口令：
  ```bash
  export GCMW_EXIT_PASSWORD='新口令'
  ```
- AIUI / VTN 配置：
  - 部署时通过本地受控文件或 Secret 注入覆盖占位符
  - 不要把真实值提交到 Git

## 4. Git 历史检查

- 当前 PR 不删除历史。
- 后续可用 `gitleaks` 或 GitHub Secret Scanning 对历史告警做登记。
- 若确认历史泄露且无法接受，需要单独走“重写历史”变更流程。
