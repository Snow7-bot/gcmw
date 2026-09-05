# Secret 清理与轮换记录

- 日期：2026-09-05
- 对应 Issue：#4
- 处理策略：方案 A（不重写 Git 历史；删除当前明文 + 外部轮换）
- 安全声明：所有曾提交到 Git 的凭据一律视为已泄露，必须先轮换再补轮换记录。

## 1. 发现与处理

| 位置 | 原值类型 | 处理 |
|---|---|---|
| `botscreen-public/src/main/index.ts` | Electron 退出口令 `***` | 改为读取 `GCMW_EXIT_PASSWORD` 环境变量 |
| ROS1 `AIUI/cfg/aiui.cfg` | appid / key / api_secret | 替换为占位符 |
| ROS2 `AIUI/cfg/aiui.cfg` | appid / key / api_secret | 替换为占位符 |
| ROS1 `AIUI/assets/vtn/vtn.ini` | appid | 替换为占位符 |
| ROS2 `AIUI/assets/vtn/vtn.ini` | appid | 替换为占位符 |

## 2. 需要外部轮换

以下值已从当前代码/配置中移除，且可能仍存在于 Git 历史中：

- Electron 退出口令
- AIUI `appid`
- AIUI `key`
- AIUI `api_secret`
- VTN `appid`

由于采用方案 A，**不重写 Git 历史**。  
因此以上所有凭据均视为已泄露，必须在对应平台完成轮换/失效，并将轮换结果登记到本文件或 Issue #4。

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
- 必须使用正式 Secret 扫描工具（如 Gitleaks/TruffleHog/GitHub Secret Scanning）扫描完整 Git 历史。
- 对固件、EXE 等二进制文件的排除必须建立“有理由、有审批记录”的规则，不能默认全排除。
- 扫描结果需脱敏后附到 PR/Issue。
- 若确认历史泄露且无法接受，需要单独走“重写历史”变更流程。
