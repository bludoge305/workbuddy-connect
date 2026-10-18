# workbuddy-connect

把已登录的 **WorkBuddy（国内版）/ WorkBuddy AI（国际版）** 桌面应用接入 ZCode 的插件：通过 MCP 工具提供模型目录、对话、积分余额与诊断，并通过本地 OpenAI 兼容桥把 WorkBuddy 模型注册进 ZCode 的模型选择器。

## 在 ZCode 中安装

打开 ZCode **插件市场 → 添加 → 添加插件市场**，粘贴本仓库地址：

```
https://github.com/Bludoge305/workbuddy-connect
```

添加后在 **个人** 找到 "WorkBuddy 接入"，点击安装。详细前置条件与使用说明见 [workbuddy-connect/README.md](workbuddy-connect/README.md)。

## 仓库结构

```text
marketplace.json              ZCode 插件市场清单（本仓库即一个市场源）
workbuddy-connect/            插件源码
└── README.md                 完整文档：安装、使用、原理、排障
```

## License

MIT，详见 [LICENSE](LICENSE)。
