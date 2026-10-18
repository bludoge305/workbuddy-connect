# workbuddy-connect（ZCode 插件）

把已登录的 **WorkBuddy（国内版）/ WorkBuddy AI（国际版）** 桌面应用接入 ZCode：零配置复用桌面登录态，通过 MCP 工具提供模型目录、对话、积分余额与诊断能力；同时由插件自带一个本地 OpenAI 兼容桥，把 WorkBuddy 模型注册成 ZCode 的普通模型供应商，直接出现在模型选择器和"模型设置"页。

协议移植自 [corrinehu/dsh-workbuddy-connect](https://github.com/corrinehu/dsh-workbuddy-connect)（MIT）的 TypeScript 实现（其上游参照 workbuddy2api，MIT），本仓库为 Python 标准库实现，唯一可选依赖是 `cryptography`（仅解密 5.6+ 加密凭据需要）。

## 功能

### MCP 工具（11 个）

| 工具 | 说明 |
|---|---|
| `workbuddy_status` | 登录态 / 账号 / 令牌有效期 /（可选）积分余额 |
| `workbuddy_models` | cli 花名册模型目录：上下文窗口、图片支持、推理档位、积分倍率、促销徽章 |
| `workbuddy_chat` | 对话（OpenAI 格式消息，支持图片分部；内部 SSE 流式聚合） |
| `workbuddy_credits` | 实时积分：套餐包明细、24h/3d/7d 过期预警、企业周期额度（`unlimited`） |
| `workbuddy_doctor` | 诊断：凭据候选与格式、Electron 定位、缓存、cryptography；`live=true` 联网自检 |
| `workbuddy_logout` | 清理插件自有刷新凭据副本（桌面应用登录不受影响） |
| `workbuddy_bridge_start` | 启动本地 OpenAI 兼容桥，并默认注册 ZCode 供应商 |
| `workbuddy_bridge_status` | 桥的运行状态、端口、PID、日志与配置文件位置 |
| `workbuddy_bridge_stop` | 停止本地桥 |
| `workbuddy_provider_register` | 重新注册/刷新供应商条目（桥未运行时会先启动） |
| `workbuddy_provider_unregister` | 只移除本插件拥有的供应商与模型规则 |

另含 skill（`workbuddy-connect`，触发与使用指引）和四个 slash 命令：`/workbuddy-status`、`/workbuddy-models`、`/workbuddy-doctor`、`/workbuddy-provider`。

### 在 ZCode 模型选择器中使用 WorkBuddy 模型

1. 运行 `/workbuddy-provider`（或直接让 Agent 调用 `workbuddy_bridge_start`）。
2. 插件会在 `127.0.0.1:38271` 启动一个只监听本机的 OpenAI 兼容服务，提供 `GET /v1/models` 与 `POST /v1/chat/completions`（支持 SSE、tools、图片输入、`reasoning_effort`）。
3. 插件把自己拥有的供应商条目写入 `~/.zcode/v2/provider_config.json`：
   - `WorkBuddy`（国内版，`api.type = openai-chat-completions`）
   - `WorkBuddy AI`（国际版，仅在该产品已登录时出现）
   - 每个模型同时写入 `contextWindow`、图片支持、工具调用等能力声明。
4. ZCode 每秒轮询该文件；重新打开模型选择器即可看到模型，模型设置页中也能像普通自定义供应商一样查看和修改。
5. 之后每次 ZCode 会话启动时，SessionStart 钩子会自动确保桥在运行并刷新注册；桥本身不写 WorkBuddy 凭据，只复用插件已有的登录态。

桥使用本地随机 bearer token，仅绑定 `127.0.0.1`；token 只是本地回环鉴权，不是 WorkBuddy 凭据。`workbuddy_bridge_stop` 可随时停止桥；`workbuddy_provider_unregister` 只删除本插件拥有的供应商与模型规则，不影响你已有的其他供应商。

桥在后台运行且**不会弹出终端窗口**。Windows 下优先用同目录的 `pythonw.exe` 启动，配合 `CREATE_NO_WINDOW` 与 `SW_HIDE`，从根源上避免控制台窗口；进程输出重定向到日志 `~/.zcode/workbuddy-connect/bridge.log`。

## 安装

### 前置条件

1. **ZCode 桌面客户端**（任意较新版本，需支持插件市场）。
2. **Python 3.10+**，且 `python` 命令在 PATH 中可用（安装时勾选 "Add python.exe to PATH"，或手动加入）。
   - 仅解密 WorkBuddy 5.6+ 加密凭据需要额外依赖：`pip install cryptography`（明文凭据不需要，可先试用，遇到报错再装）。
3. **已登录的 WorkBuddy / WorkBuddy AI 桌面应用**（插件复用其登录态，自身不提供登录）。

### 通过插件市场安装（推荐）

1. 打开 ZCode 的 **插件市场 / Plugin Marketplace**。
2. 点击 **添加 / Add → 添加插件市场 / Add Plugin Marketplace**。
3. 粘贴本仓库地址：

   ```
   https://github.com/Bludoge305/workbuddy-connect
   ```

4. 添加成功后，在 **个人 / Personal** 找到 "WorkBuddy 接入"，点击 **安装 / Install**。
5. 安装后在 **设置 → 插件 / Settings → Plugins** 确认已启用。

> 提示：也可以先用 `git clone https://github.com/Bludoge305/workbuddy-connect.git` 到本地，然后把**仓库根目录**（包含 `marketplace.json` 的那一层）粘贴进"添加插件市场"，效果相同。

### 安装后第一次使用

1. 确认 WorkBuddy 桌面应用已登录（插件不自己登录）。
2. 在 ZCode 新开一个会话，发送：

   ```
   查看 WorkBuddy 的登录状态和可用模型
   ```

   预期：Agent 调用 `workbuddy_status` + `workbuddy_models`，报告登录账号、令牌有效期和模型列表（含积分倍率）。
3. 想把模型加进模型选择器，发送：

   ```
   把 WorkBuddy 模型注册进 ZCode 模型选择器
   ```

   预期：本地桥启动，`~/.zcode/v2/provider_config.json` 出现 `WorkBuddy` / `WorkBuddy AI` 供应商；重新打开模型选择器即可选用。
4. 对话测试（消耗积分，建议先用免费模型，如国内版 `hy3`）：

   ```
   用 hy3 模型回答：你好
   ```

## 工作原理

1. **凭据复用**：按平台探测桌面应用的登录文件（Windows 依次探测 `%LOCALAPPDATA%`、`%APPDATA%` 下的 `CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`，两版产品共用目录、仅文件名不同：国际版为 `workbuddy-desktop-ai.info`）。
2. **静止加密**：WorkBuddy 5.6+ 将 `accessToken`/`refreshToken` 以 `{$wbEncrypted:1, envelope}` 信封存储（AES-256-GCM，suite 1）。插件以 `ELECTRON_RUN_AS_NODE=1` 运行一次应用自己的可执行文件，经其私有 `workbuddyStorage` 绑定拿到 at-rest secret，`sha256` 派生 protector key 后解密。定位顺序：环境变量 → 默认安装路径（CN 已测量）→ Windows 卸载注册表 + 安装布局校验。
3. **只读桌面 + 插件自有副本**：桌面文件永不写回；token 刷新结果写入 `~/.zcode/workbuddy-connect/` 下的自有副本（0600）。同一账号取较新者，**身份以桌面文件为准**（换号后旧副本不会冒充新账号）。
4. **区域隔离**：按凭据 `domain` 判定 cn/global（`workbuddy.ai` 及子域为国际版），跨区凭据直接拒绝并指出修复方法；两版的模型花名册、积分、目录缓存互相独立。
5. **上游接口**：chat `POST /v2/chat/completions`（强制 SSE、`developer`→`system`、`tool_choice` 扁平化、剔除 `reasoning_effort:"off"`、国际版前置 system）；刷新 `POST /v2/plugin/auth/token/refresh`；目录 `GET /v3/config`（CN 带 CLI UA + 账号身份头，国际版带 `WorkBuddyAI/<v>` App UA；CN 另从 console 文档合并促销徽章）；积分 `POST /v2/billing/meter/get-user-resource`（CN 企业走 `get-enterprise-user-usage`）。
6. **目录降级链**：实时 → 30 分钟缓存 → 过期缓存（标注 stale）→ 内置快照（仅未登录且无缓存时，明确标注不是实时列表）。

## 环境变量（全部可选）

| 变量 | 作用 |
|---|---|
| `WORKBUDDY_AUTH_FILE` | 覆盖国内版桌面凭据文件路径 |
| `WORKBUDDY_AI_AUTH_FILE` | 覆盖国际版桌面凭据文件路径 |
| `WORKBUDDY_ELECTRON_BIN` | 国内版应用可执行文件（加密凭据解密用） |
| `WORKBUDDY_AI_ELECTRON_BIN` | 国际版应用可执行文件 |
| `WORKBUDDY_CONNECT_HOME` | 插件状态目录（默认 `~/.zcode/workbuddy-connect/`） |
| `WORKBUDDY_PYTHON` | 桥启动使用的 Python 解释器路径（默认 `python`，走 PATH） |

MCP server 默认用 PATH 中的 `python` 启动（见 `.mcp.json`）；如果你的 Python 不在 PATH，或想固定用某个虚拟环境，设置 `WORKBUDDY_PYTHON` 为该解释器的绝对路径（SessionStart 钩子会读取它）。

## 排障

1. 先跑 `/workbuddy-doctor`（不加 live）：确认凭据文件是否找到、`format` 是 `plaintext`/`encrypted`/`absent`/`unrecognized`、Electron 是否定位、cryptography 是否可用。
2. `signed-out` 且 `reasonCode: no-credential` → 在桌面应用登录一次。
3. `credential-region-mismatch` → 环境变量指错了版本（WorkBuddy 与 WorkBuddy AI 共用目录、文件名不同）。
4. 聊天报 `session_dead` → 打开桌面应用一次让它续签；报 `hard_credit` → 积分不足；`soft_rate` → 限流，稍后重试。
5. 插件副本与桌面登录不一致（换号后身份错乱）→ `workbuddy_logout` 清副本，下次请求会以桌面文件为准重建。
6. 加密凭据解密失败并提示缺少 cryptography → 在插件使用的 Python 环境执行 `pip install cryptography`。

## 安全说明

- 令牌不写日志、不出现在工具输出；错误消息只带路径、id 与大小。
- 本地桥只绑定 `127.0.0.1`，并要求随机 bearer token；token 保存在插件状态目录的 `bridge.json`，不是 WorkBuddy 凭据，也不会发给上游。
- 注册供应商时只替换 `providerId` 属于本插件的条目；首次修改前在 `~/.zcode/workbuddy-connect/provider_config.backup.json` 保留一份原始配置备份。
- 上游为私有客户端接口，非官方开放 API，WorkBuddy 更新后可能需要调整；仅供个人学习研究使用，与腾讯 / WorkBuddy / DeepSeek 无关联。

## 目录结构

```text
workbuddy-connect/
├── .zcode-plugin/plugin.json     插件清单
├── .mcp.json                     MCP server 配置（python 走 PATH）
├── marketplace.json              插件市场清单（供"添加插件市场"使用）
├── mcp/
│   ├── workbuddy_mcp.py          MCP stdio 服务（JSON-RPC，11 个工具）
│   ├── wb_bridge.py              本地桥的按路径启动入口
│   └── wbclient/
│       ├── variants.py           产品变体：路径、区域、Electron 定位参数
│       ├── catalog.py            目录解析 + 内置兜底花名册
│       ├── auth.py               凭据读取/5.6 解密/自有副本/按需刷新
│       ├── upstream.py           HTTP 客户端：刷新/目录/积分/对话 SSE
│       ├── bridge.py             本地 OpenAI 兼容桥（/v1/models、/v1/chat/completions）
│       ├── zcode_provider.py     写入/移除 ZCode 个人供应商配置
│       ├── doctor.py             诊断装配
│       └── selftest.py           离线自测（python mcp/wbclient/selftest.py）
├── hooks/
│   ├── hooks.json                SessionStart：确保桥运行并刷新注册
│   └── ensure-bridge.cmd         Windows 启动包装（可用 WORKBUDDY_PYTHON 覆盖解释器）
├── skills/workbuddy-connect/SKILL.md
├── commands/                     /workbuddy-status · /workbuddy-models · /workbuddy-doctor · /workbuddy-provider
└── README.md
```

## 开发与自测

```bash
# 离线自测（不需要登录桌面应用）
python mcp/wbclient/selftest.py
```

覆盖：5.6 信封加解密往返、凭据文档分类与解析、chat body 归一化、目录解析与促销重估、积分到期分桶。

已知限制：国际版（WorkBuddy AI）路径未在真实环境验证；CN 企业账号周期额度接口未验证；WorkBuddy 上游对 `response_format` 的支持情况未知（桥默认关闭该能力声明）。WorkBuddy 客户端更新后协议可能变化，出现异常先跑 `/workbuddy-doctor`。

## License

MIT（协议实现参照 [corrinehu/dsh-workbuddy-connect](https://github.com/corrinehu/dsh-workbuddy-connect)，MIT）。
