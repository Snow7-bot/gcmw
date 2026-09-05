# Gitleaks Git 全历史扫描报告（脱敏）

- 扫描时间：2026-09-05
- 扫描命令：`gitleaks git . --redact --report-format json --exit-code 0`
- 扫描范围：完整 Git 历史
- 扫描提交数：33
- 扫描字节：约 39 MB

## 结果汇总

| 指标 | 数量 |
|---|---|
| 发现总数 | 286 |
| 规则数 | 1 |

## 规则分布

| RuleID | 数量 |
|---|---|
| generic-api-key | 286 |

## 文件分布（仅计数）

| 文件 | 数量 |
|---|---|
| ROS1 `AIUI/msc/aiui.log` | 237 |
| ROS2 `AIUI/msc/aiui.log` | 43 |
| ROS1 `AIUI/cfg/aiui.cfg` | 3 |
| ROS2 `AIUI/cfg/aiui.cfg` | 3 |

## 处置结论

- 所有发现均来自 AIUI SDK 历史日志或配置文件。
- 已按方案 A 删除当前工作区明文，并替换为占位符。
- 历史中存在的值视为已泄露，必须由真实凭据负责人完成轮换。
- 本报告不包含任何真实凭据内容。
- 后续 Gitleaks Action 负责事件驱动持续检查，不能替代本独立全历史扫描。
