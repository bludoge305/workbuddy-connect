---
description: 启动 WorkBuddy 本地模型桥，并把 WorkBuddy 模型注册进 ZCode 模型选择器
---

调用 `workbuddy_bridge_start` 工具，参数默认 `register: true`、`variant: "all"`。如果用户要求刷新模型目录，再加 `refresh: true`。

然后按以下结构汇报：

- 桥接服务：是否运行、监听地址（`http://127.0.0.1:<port>/v1`）、PID、日志路径。
- 已注册的供应商：国内 WorkBuddy / 国际 WorkBuddy AI 分别注册了多少个模型；列出主要模型 id。
- 配置写入位置：`~/.zcode/v2/provider_config.json`，说明只增改本插件拥有的条目，原有供应商保留。
- 告诉用户：ZCode 每秒轮询该配置，若模型选择器没有立即出现，重新打开模型选择器或新开一个会话即可；模型会显示在“模型设置 → 自定义供应商”下，可像普通供应商一样查看和配置。
- 如果注册失败，读取错误信息并调用 `workbuddy_bridge_status`、`workbuddy_doctor` 诊断；凭据问题提示用户先在 WorkBuddy 桌面端登录。

$ARGUMENTS
