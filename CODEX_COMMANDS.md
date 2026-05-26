# Codex 常用命令速查（CLI + 斜杠命令）

本文件把 Codex 里你可能会用到的“命令行”分两类整理：

1. `codex ...`：在系统终端（PowerShell/cmd/bash）里运行的命令。
2. `/...`：进入 Codex 交互式 TUI 之后，在输入框里键入的斜杠命令（不是系统 shell 命令）。

注：如果你在 PowerShell 里运行 `codex` 报“禁止运行脚本”，可先执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

---

## 1) 斜杠命令（在 Codex TUI 中输入）

这些命令用于控制当前会话（模型、权限、状态、diff、恢复历史等）。

| 命令 | 作用 |
| --- | --- |
| `/model` | 选择当前线程使用的模型（以及支持时的 reasoning effort）。 |
| `/fast` | 切换当前模型的 Fast 服务档位（`on/off/status`，是否可用取决于模型目录）。 |
| `/status` | 显示会话配置与 token/上下文使用情况（确认当前模型、审批策略、可写目录、剩余上下文等）。 |
| `/plan` | 进入 Plan 模式（可附带提示词），让 Codex 先出执行计划再动手。 |
| `/goal` | 设置/暂停/恢复/查看/清除“目标”（让 Codex 在长任务里持续对齐目标）。 |
| `/personality` | 切换回复风格（例如更简洁/更解释型/更协作型；支持时显示）。 |
| `/ps` | 查看实验性的后台终端及其最近输出（适合长命令监控）。 |
| `/stop` | 停止所有后台终端（取消当前会话启动的后台命令）。 |
| `/compact` | 压缩对话记录：用摘要替换早期回合，释放上下文。 |
| `/diff` | 在 TUI 中查看当前工作区的 Git diff（含未跟踪文件提示）。 |
| `/mention <path>` | 把文件/路径“点名”加入对话上下文（从弹窗选择匹配路径）。 |
| `/new` | 在同一个 CLI 会话里开启一段新的对话线程（不等同于清屏）。 |
| `/clear` | 清空当前终端视图（文档提到 `/new` 与 `/clear` 的区别）。 |
| `/resume` | 从会话列表里恢复一段保存的对话。 |
| `/fork` | 把当前对话 fork 成新线程（保留原线程不变，用于探索替代方案）。 |
| `/side` | 开启“侧边对话”（临时分支，不切走主线程；适合快速问一个不打断主流程的问题）。 |
| `/raw` | 切换 raw scrollback 模式（更适合选择/复制长输出）。 |
| `/review` | 让 Codex 对当前工作区做一次 review（通常在改完代码后用）。 |
| `/debug-config` | 输出配置层级与需求诊断（排查 config.toml 覆盖/策略要求/网络约束等）。 |
| `/permissions` | 调整 Codex 在不询问的情况下可以做什么（更改权限/审批相关）。 |
| `/approve` | 手动批准/重试一次先前被自动审核拒绝的动作（仅在需要重试时用）。 |
| `/statusline` | 交互式配置 TUI 底部状态栏字段（并可持久化到 `config.toml`）。 |
| `/title` | 交互式配置终端窗口/标签标题字段（并可持久化到 `config.toml`）。 |
| `/theme` | 选择语法高亮主题（预览并持久化）。 |
| `/quit` 或 `/exit` | 退出 Codex CLI（离开会话）。 |

---

## 2) `codex` 终端命令（在系统 shell 中运行）

这些是 `codex --help` 输出的子命令，用于登录、更新、非交互执行、恢复历史等。

| 命令 | 作用 |
| --- | --- |
| `codex` | 启动交互式 Codex TUI。 |
| `codex -m <MODEL>` | 启动时指定模型（例如 `codex -m gpt-5.5`）。 |
| `codex exec ...` | 非交互模式运行 Codex（适合脚本化/CI/自动化）。 |
| `codex review` | 非交互地对仓库做 code review。 |
| `codex login` | 登录（可走浏览器/设备授权/从 stdin 读取 key/token）。 |
| `codex login status` | 查看当前 CLI 登录状态。 |
| `codex logout` | 退出并清除本机保存的认证信息。 |
| `codex update` | 更新 Codex CLI 到最新版本。 |
| `codex doctor` | 诊断安装、配置、认证、网络连通性等问题。 |
| `codex resume` | 从保存的会话里恢复（也可用 `--last`）。 |
| `codex fork` | 从保存的会话里 fork（也可用 `--last`）。 |
| `codex apply` | 把 Codex 产生的最新 diff 以 `git apply` 形式应用到当前工作区。 |
| `codex mcp ...` | 管理 MCP 服务器（给 Codex 接第三方工具/上下文）。 |
| `codex plugin ...` | 管理 Codex plugins。 |
| `codex completion` | 生成 shell 自动补全脚本。 |
| `codex sandbox ...` | 在 Codex 提供的 sandbox 策略下运行命令。 |
| `codex debug ...` | 调试工具集合（排查内部状态/问题）。 |
| `codex app` | 启动 Codex 桌面 App（缺失会触发安装流程）。 |
| `codex app-server` | 运行 app server（实验特性）。 |
| `codex remote-control` | 管理启用了 remote control 的 app-server（实验特性）。 |
| `codex cloud` | 浏览 Codex Cloud 任务并把改动应用到本地（实验特性）。 |
| `codex exec-server` | 运行独立 exec-server 服务（实验特性）。 |
| `codex features` | 查看/管理 feature flags。 |

---

## 3) 常见用法示例

### 启动并指定模型

```powershell
codex -m gpt-5.5
```

### 进入后查看当前会话配置/限额

在 TUI 输入：

```text
/status
```

### 切换模型后确认生效

在 TUI 输入：

```text
/model
/status
```
