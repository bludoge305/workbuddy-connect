---
description: 查看 WorkBuddy / WorkBuddy AI 的登录状态与积分余额
---

调用 `workbuddy_status` 工具（variant=all, includeCredits=true），把结果整理成简表汇报：

1. 每个产品的登录状态、账号昵称、凭据来源（desktop/plugin）、令牌过期时间（换算成本地时间）。
2. 若 includeCredits 成功：总积分、各套餐包余量与到期时间，如有 24h/3d/7d 内到期的积分要醒目提示；企业账号的 `unlimited` 周期额度要说明"不设上限"而不是"0"。
3. 任何 `signed-out` 或报错：给出原因（reasonCode）和下一步建议（如"在桌面应用登录一次"），不要猜。

$ARGUMENTS
