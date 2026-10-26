# 安全策略

## 报告漏洞

通过 GitHub Security 页的 Report a vulnerability 入口私密提交，不要在公开 issue 或 PR 里讨论漏洞细节。收到报告后尽快确认并修复，修复发布后在 release notes 致谢报告者（除非要求匿名）。

## 支持范围

只维护 `main` 分支的最新版本。

## 数据流向

本工具把选中的终端文本发送给你配置的翻译服务。默认后端顺序为 LLM、MyMemory、LibreTranslate、Google，任一失败就回退到下一个，因此文本可能依次到达多个第三方服务。不需要联网翻译时，把 `GTRANS_BACKENDS` 设为单个后端，或在控制面板里关闭开关。

LLM 密钥保存在 `~/.config/ghostty-translate/llm.json`，权限 0600，不写入仓库。弹出层通过 `/run/user/<uid>/ghostty-translate-<uid>.sock` 接收文本，socket 权限 0600。
