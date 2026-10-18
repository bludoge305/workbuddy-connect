---
description: 诊断 WorkBuddy 连接问题（凭据、Electron、缓存、联网自检）
---

调用 `workbuddy_doctor` 工具做诊断。用户报告了具体故障、或要求彻底检查时加 `live: true`（会发起真实目录/积分请求）。

按以下顺序解读并汇报：

1. `stateHome` 与 `cryptographyModule`：缺失 cryptography 说明 5.6+ 加密凭据解不了（明文不受影响），给出 `pip install cryptography` 的建议。
2. 每个 variant：`auth` 状态与 `authFile`（哪个候选路径存在、format 是 plaintext/encrypted/absent/unrecognized）；`unrecognized` 要建议修复或删除该文件。
3. `electron`：只在凭据是 encrypted 时才关键；未配置时给出 env 变量名（WORKBUDDY_ELECTRON_BIN / WORKBUDDY_AI_ELECTRON_BIN）与应指向的 exe。
4. `catalogCache` 与（如有）`live` 结果：目录来源、模型数、实时检查哪项失败。
5. 给出明确的下一步动作清单（如"打开 WorkBuddy 桌面应用登录一次"），不确定的地方如实说明。

$ARGUMENTS
