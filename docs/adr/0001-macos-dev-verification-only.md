# ADR-0001: 首版仅将 macOS 作为开发验证平台（macOS 打包/签名策略）

- 日期：2026-09-07
- 状态：提议
- 决策人：Technical Lead、Security/SRE、Change Authority（打包/签名治理属工程与安全决策；临床签字不适用本 ADR）
- 相关 Issue：#42（SCA-3，PR #48）

## 背景

electron-builder 自 26.15 起取消了隐式 ad-hoc 签名 fallback（上游 electron-userland/electron-builder#9822）。实测：

- builder 26.8.1 产物：自动 ad-hoc 签名，`codesign --verify --deep --strict` 通过；
- builder 26.16.0 产物：跳过应用签名，同一验证退出 1（仅 Electron 框架 linker-signed adhoc）。

因此 `electron-builder --dir` 退出码 0 只代表打包完成，不代表 macOS 产物可分发或可验证签名。当前团队没有 Developer ID 证书、notarization 通道，且**尚无经批准的 macOS 生产发布范围**（生产 OS 未确定）。

## 决策

1. 首版（试点/生产候选 W20–W28）**macOS 仅作为开发验证平台**：允许使用 unpacked 产物做本地开发与链路验证，不承诺 macOS 作为受支持的生产终端平台。
2. **生产 OS 尚未选定**：CI 当前只证明 Linux x64 `electron-builder --dir` unpacked 打包成功，不代表生产 OS 已选定；Windows 打包未实机验证（macOS 签名行为变化不适用于 Windows，Windows 验证另行立项）；macOS 仅本地复核。
3. 若未来支持 macOS 生产，必须另行完成：Developer ID 证书与 provisioning、notarization 接入、`forceCodeSigning` 配置、公证后产物验证，以及对应 CI/发布通道；**不得把 `identity: "-"` 或缺失签名当作生产方案**。
4. 该边界写入发布/试点门禁：任何把 macOS 产物列为生产交付物的工作项，需先经本 ADR 修订评审。
5. **机器人实际终端清单完成后触发本 ADR 复审**：届时以清单中的真实 OS/CPU 为准重新确认各平台打包/签名要求，再决定是否升级为"已接受"。

## 后果

- 正面：不把未签名产物误当作可分发制品；SCA-3 升级不再被 macOS 签名问题阻塞；生产 OS 结论推迟到终端清单证据后作出。
- 负面：macOS 上运行本系统的终端暂不受支持；如需支持需追加签名/公证工程与预算。

## 替代方案

- 保留 26.8.1 以维持隐式 ad-hoc：不可行——旧版本存在 critical tar 供应链漏洞且不再获得安全更新，且隐式 ad-hoc 本就不构成可分发签名。
- 本轮引入 Developer ID/notarization：范围外——无证书且尚无经批准的 macOS 生产发布范围，不应为占位行为引入生产签名义务。

## 参考

- electron-builder 26.16.0 Release：https://github.com/electron-userland/electron-builder/releases/tag/electron-builder%4026.16.0
- 上游行为变更 PR：https://github.com/electron-userland/electron-builder/pull/9822
