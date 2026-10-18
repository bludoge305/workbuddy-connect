---
description: 列出 WorkBuddy 当前账号可用的模型目录
---

调用 `workbuddy_models` 工具列出模型目录，整理成表格汇报。参数处理：

- 用户指定了版本（国内/国际）或明确要求刷新时，设置对应的 `variant` / `refresh: true`，否则用默认（cn，缓存 30 分钟）。
- 表格列：id、名称、上下文窗口、最大输出、图片支持、推理档位（supportedEfforts 与默认档）、积分倍率（`x0.00` 标注"免费"，促销徽章附注）。
- 说明 `source` 字段：实时目录（workbuddy:cli / workbuddy-ai:app）、缓存、过期缓存或内置快照，让用户知道列表的新鲜度。
- 用户想直接用某个模型时，提醒模型 id 就是 `workbuddy_chat` 的 `model` 参数。

$ARGUMENTS
