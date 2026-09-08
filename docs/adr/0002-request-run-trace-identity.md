# ADR-0002: request_id / idempotency_key / trace_id 语义与身份派生

- 日期：2026-09-07
- 状态：提议
- 决策人：Technical Lead、Security/SRE、Change Authority（**待真实确认后更新为已接受**）
- 相关 Issue：#36（Agent API）；对齐 V2.3 §13 可观测性

## 背景

#64 复审发现请求身份字段混用：旧设计用 `request_id` 做幂等去重，且 API 接受 body 中的 `tenant_id/device_id`。V2.3 要求每个请求携带 trace/request/run 标识并具备明确语义；身份必须来自认证而非请求正文。

## 决策

1. **字段语义单一化**
   - `request_id`：单次 HTTP 请求的关联标识（可重复请求各次不同）；由服务端生成或接受白名单格式的客户端值；**只用于追踪，不参与幂等**。
   - `idempotency_key`：客户端为"逻辑 Run 重试"提供的幂等键；服务端以 `(session_id, idempotency_key, payload_hash)` 判定——同键同 payload 返回原 Run（重放安全），同键异 payload 返回冲突。
   - `trace_id`：端到端追踪标识（跨 API/Agent/工具/模型网关），随请求生成一次并贯穿。
2. **身份派生**：`tenant_id`/`device_id` 一律从认证的 `DevicePrincipal` 派生；API 请求体不接受也不信任这两个字段（V2.3 原协议的旧写法以此为准废弃）。
3. **会话属性继承**：`channel`/`locale` 是会话固定属性，Run 从会话继承，不得自带（语音 Run 不能挂载在文本会话下）。
4. **状态权威**：Run 生命周期状态与 SSE 事件序列的唯一权威为 `RunStateMachine` + `RunAdmissionService`（状态迁移与 SSE 追加在同一临界区，SSE seq 取自 transition 返回值）；`RunCoordinator`/`RunIdempotencyRegistry`（request_id 键）已删除，任何按 request_id 去重或第二套状态机不得重现。
5. **过期与快照**：会话 TTL 由注入时钟强制执行；过期即清除会话、其 Run、幂等记录与原始问题快照（原始文本不落指纹，仅存于短生命周期快照）。

## 后果

- 正面：身份/幂等/追踪语义可测试且不可混用；并发下每会话单活动 Run、幂等单 Run 语义成立；原始输入生命周期受 TTL 控制。
- 负面：旧协议客户端若仍传 body tenant/device 将收到 400/403；需在纵切（#55）时同步前端适配。

## 替代方案

- 以 request_id 键幂等（旧 RunCoordinator）：与"重试安全"冲突（重试携带新 request_id 将重复创建），否决。
- body 携带身份：易伪造且绕过认证，否决。

## 参考

- V2.3 §9.2（幂等键）、§13.1（trace/request/run 标识）
- Issue #36 复审结论（本 ADR 前身）
