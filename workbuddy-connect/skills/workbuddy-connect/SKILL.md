---
name: workbuddy-connect
description: Connect to the signed-in WorkBuddy / WorkBuddy AI desktop app through the workbuddy-connect MCP tools. Use when the user asks about WorkBuddy models, WorkBuddy sign-in/status/credits/balance (积分/余额), wants to chat with a WorkBuddy model (GLM, Kimi, DeepSeek, MiniMax, GPT-5.6, Gemini via WorkBuddy), wants WorkBuddy models to appear in ZCode's model picker or model settings, or needs to debug the WorkBuddy connection. 接入 WorkBuddy、查看模型、查积分、用 WorkBuddy 模型对话、把 WorkBuddy 模型注册进 ZCode 模型选择器时使用。
---

# WorkBuddy Connect

本插件复用已登录的 WorkBuddy（国内版）/ WorkBuddy AI（国际版）桌面应用的登录态，为 ZCode 提供十一个 MCP 工具。两个产品共用一个 `CodeBuddyExtension` 凭据目录、仅文件名不同；账号、模型、积分互不混用，凭据跨区会被拒绝并给出修复提示。

协议移植自 [corrinehu/dsh-workbuddy-connect](https://github.com/corrinehu/dsh-workbuddy-connect)（MIT License，Copyright © 2026 Corrine Hu），其上游参照 [Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)（MIT）。

## 工具一览

| 工具 | 用途 | 网络 |
|---|---|---|
| `workbuddy_status` | 登录态、账号昵称、令牌有效期、（可选）积分余额 | 可选 |
| `workbuddy_models` | cli 花名册模型目录（含上下文窗口/图片支持/推理档位/积分倍率） | 可缓存 |
| `workbuddy_chat` | 用某个模型对话（内部 SSE 聚合，消耗积分） | 是 |
| `workbuddy_credits` | 实时积分余额（个人套餐包/企业周期额度） | 是 |
| `workbuddy_doctor` | 凭据路径/格式、Electron 定位、缓存、`live=true` 加联网自检 | 可选 |
| `workbuddy_logout` | 清理插件自有的刷新凭据副本（不动桌面应用的登录） | 否 |
| `workbuddy_bridge_start` | 启动本地 OpenAI 兼容桥并注册 ZCode 供应商 | 否 |
| `workbuddy_bridge_status` | 桥状态、端口、PID、日志、配置路径 | 否 |
| `workbuddy_bridge_stop` | 停止本地桥 | 否 |
| `workbuddy_provider_register` | 重新注册/刷新模型条目（桥未运行会先启动） | 可选 |
| `workbuddy_provider_unregister` | 只移除本插件拥有的供应商与模型规则 | 否 |

## 在 ZCode 模型选择器中使用

ZCode 的模型选择器只认 `~/.zcode/v2/provider_config.json` 里声明的供应商。插件通过本地 OpenAI 兼容桥把 WorkBuddy 模型接进去：

1. 需要时调用 `workbuddy_bridge_start`（默认同时注册；`/workbuddy-provider` 命令等价）。
2. 桥监听 `127.0.0.1:38271`，提供 `GET /v1/models` 和 `POST /v1/chat/completions`，支持 SSE、tools、图片输入和 `reasoning_effort`。
3. 插件写入 `WorkBuddy`（国内版）与 `WorkBuddy AI`（国际版，仅登录时）两个自定义供应商，模型带上下文窗口、图片支持和工具调用能力声明。
4. ZCode 每秒轮询该文件；重新打开模型选择器即可看到。模型设置页中也能像普通供应商一样查看和修改。
5. 桥使用本地随机 bearer token，只绑定 `127.0.0.1`；SessionStart 钩子会在新会话自动确保桥运行并刷新注册。

注意：通过模型选择器直接使用 WorkBuddy 模型会消耗 WorkBuddy 积分（对话在 ZCode 内完成）；这与 `workbuddy_chat` 工具走的是同一套上游接口。

## 使用要点

- **先看目录再对话**：模型 id 以上游目录为准（如 `glm-5.3`、`kimi-k3-2`），花名册会随上游增减（CN 曾一周内出现又移除过 `auto`/`kimi-k3-1`/`minimax-m3`）。`workbuddy_models` 返回的 `source` 字段说明目录来源：`workbuddy:cli`/`workbuddy-ai:app`（实时）、`cache`（30 分钟内）、`cache-stale`（过期缓存）、`builtin-fallback`（内置快照，仅在未登录且无缓存时）。
- **推理档位**：只有目录声明了 `supportedEfforts` 的模型才可传 `reasoningEffort`（low/medium/high/xhigh/max）；不要给未声明的模型传档位。`reasoning_effort: "off"` 会被自动剔除（两个区域网关都拒绝它）。
- **积分倍率**：`billing.credits` 如 `x0.79`，`x0.00` 表示当前免费（`free: true`）；促销徽章在 `billing.badges`（如 限时免费/夜间折扣）。国际版的限时促销按当前时间重估，过期后显示 `rateUnknown: true`（费率未知，需刷新），绝不继续声称免费。
- **积分余额**：个人账号返回套餐包明细 + 24h/3d/7d 过期预警；CN 企业账号走周期额度接口，`unlimited: true` 表示不设上限（此时不要把 `total: 0` 当成"没有积分"）。
- **图片输入**：`supportsImages: true` 的模型才可在 `messages` 里传 OpenAI 格式的 `image_url` 分部；不确定的模型按纯文本处理，避免服务端在消息落库后才拒绝。
- **国际版前置 system**：global 网关要求首条消息是 system，服务端会自动补一条（不需要调用方处理）。
- **variant 选择**：`workbuddy_chat` 不传 `variant` 时按"国内版优先、其次国际版"自动尝试已登录的产品；两版模型 id 有重叠（如 `glm-5.3`、`hy3` 两边都有但倍率不同），明确场景时建议显式指定。

## 配置（可选）

全部自动探测，无需配置。仅特殊情况需要：

- `WORKBUDDY_AUTH_FILE` / `WORKBUDDY_AI_AUTH_FILE`：指向桌面应用凭据文件（默认探测 `%LOCALAPPDATA%` 与 `%APPDATA%` 下的 `CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` / `workbuddy-desktop-ai.info`）。
- `WORKBUDDY_ELECTRON_BIN` / `WORKBUDDY_AI_ELECTRON_BIN`：指向应用可执行文件，仅在 5.6+ 加密凭据需要解密且自动定位失败时设置（Windows 会先查默认安装路径再查卸载注册表）。
- `WORKBUDDY_CONNECT_HOME`：插件自有状态目录（默认 `~/.zcode/workbuddy-connect/`，存放刷新后的凭据副本与目录缓存）。

插件 Python 环境需可 `import cryptography`（仅解密 5.6+ 加密凭据需要；明文凭据不需要）。

## 排障

1. 先跑 `workbuddy_doctor`（不加 live）：确认凭据文件是否找到、`format` 是 `plaintext`/`encrypted`/`absent`/`unrecognized`、Electron 是否定位、cryptography 是否可用。
2. `signed-out` 且 `reasonCode: no-credential` → 在桌面应用登录一次。
3. `credential-region-mismatch` → 环境变量指错了版本（WorkBuddy 与 WorkBuddy AI 共用目录、文件名不同）。
4. 聊天报 `session_dead` → 打开桌面应用一次让它续签；报 `hard_credit` → 积分不足；`soft_rate` → 限流，稍后重试。
5. 插件副本与桌面登录不一致（换号后身份错乱）→ `workbuddy_logout` 清副本，下次请求会以桌面文件为准重建。

## 安全与合规

- 凭据只读桌面文件；刷新结果写入插件自有副本（0600 权限）；任何令牌不进日志、不出现在工具输出里。
- 上游是私有客户端接口（协议经 [corrinehu/dsh-workbuddy-connect](https://github.com/corrinehu/dsh-workbuddy-connect) 移植，MIT；其上游参照 workbuddy2api，MIT），WorkBuddy 更新后可能需要调整；仅限个人学习研究使用，与腾讯/WorkBuddy 无关联。
