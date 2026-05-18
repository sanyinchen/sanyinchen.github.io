---
title: "AgentPlanFlow：多智能体协作编程的桌面工作台"
author: sanyinchen
date: 2026-05-16
categories: [ AI, 工作站 ]
tags: [AgentPlanFlow, AI-Agent, Codex, Claude, Hermes, 架构, 多智能体]
render_with_liquid: false
toc: true
---

## 一、项目概述

AgentPlanFlow 是一个面向多智能体协作编程的桌面工作台。它把 **Codex**（OpenAI）、**Claude Code**（Anthropic）和 **Hermes**（Nous Research）组织在同一个原生桌面窗口里，让不同智能体可以围绕同一工作目录运行、协作、委托任务，并共享执行上下文。

它并不是简单地把几个终端拼在一起。AgentPlanFlow 在桌面侧负责窗口、终端、进程和跨面板控制，在服务侧提供模型 API 代理、请求记录、控制台输出和 Trace 事件管理。这样一来，用户既可以直接操作每个智能体，也可以让 Hermes 拆解任务后，把子任务委托给 Codex 或 Claude，再把结果带回编排流程。

AgentPlanFlow 还集成了 **Codex App / Codex Desktop**。桌面层会启动 `codex-desktop`，再通过 X11 窗口定位与同步机制，把 Codex App 视觉嵌入到 GTK 工作台左侧区域。用户可以在 Codex App 中保留完整的图形化交互体验，同时继续使用 Codex CLI 面板参与 Hermes 的自动委托链路。

各面板的职责如下：

| 面板 | 运行对象 | 主要职责 |
|------|----------|----------|
| **Codex App** | OpenAI Codex Desktop | 图形化 Codex 交互入口，视觉嵌入 GTK 工作台 |
| **Codex** | OpenAI Codex CLI | 代码生成、修复和局部实现，可被 Hermes 委托 |
| **Claude Code** | Anthropic Claude CLI | 代码审查、重构建议和复杂推理 |
| **Hermes** | Nous Research Hermes CLI | 任务拆解与编排，可通过 `ask_codex` / `ask_claude` 委托子任务 |
| **Trace** | 内置 Trace / Console 面板 | 展示代理请求、控制台输出和任务事件 |

这个设计的核心目标，是把不同智能体的优势放进同一个开发工作流：Codex 偏向快速实现，Claude 偏向审查和解释，Hermes 则负责拆解、调度和串联结果。

---

## 二、技术栈

| 层次 | 技术选型 | 工程结构 |
|------|----------|----------|
| 桌面 GUI | Python 3 + GTK3 + VTE | `index.py`、`app/` |
| Codex 桌面嵌入 | X11 窗口嵌入与同步 | `app/embed/` |
| 跨面板控制 | PTY + Unix Domain Socket + JSON | `app/control_server.py`、`service/hermes/bridge/` |
| API 代理服务 | Rust + Tokio + Axum | `service/proxy/` |
| Web 仪表盘 | Rust + Axum + SSE | `service/proxy/crates/web_app/` |
| 终端仪表盘 | Rust + Ratatui | `service/proxy/crates/tui_app/` |
| Python 绑定 | PyO3 | `service/proxy/crates/python_api/` |
| 容器化 | Docker + Compose | `docker/` |

核心依赖关系可以概括为：

- `index.py` 是 GTK3 桌面应用入口，负责创建窗口、读取配置、启动 Codex、Claude、Hermes 和 Trace 面板。
- `CodexEmbedder` 负责启动并管理 Codex App，把 `codex-desktop` 作为独立 X11 顶层窗口固定到 GTK 嵌入区。
- Codex、Claude、Hermes 分别运行在独立 PTY 中，保留各自原生 CLI 的认证、会话和交互体验。
- Hermes 通过 Bridge Plugin（`agentplanflow-bridge`）访问本机 Unix Socket，把任务发送到 Codex 或 Claude 面板。
- Rust Proxy Service 提供 OpenAI / Anthropic 风格的兼容路由，并把请求、响应和控制台输出推送到观测层。

---

## 三、系统架构

### 3.1 整体架构图

下图展示了 AgentPlanFlow 从桌面窗口到上游模型服务的整体链路。

![AgentPlanFlow 整体架构图](/assets/img/2026-05-16-AgentPlanFlow/architecture-overview.bordered.webp)

架构可以分为四层：

1. **桌面窗口层 Desktop**：GTK3 + VTE 承载 Codex App、Codex CLI、Claude Code、Hermes 和 Trace。Codex App 通过 X11 视觉嵌入固定在桌面工作台中，其他智能体则以 PTY 终端面板运行。
2. **服务应用层 Service App（Python）**：负责 PTY 进程管理、Unix Socket 控制服务和面板输出采集，是桌面窗口和智能体 CLI 之间的协调层。
3. **代理服务层 Proxy Service（Rust）**：负责 API 代理、请求记录、Trace 事件、Web / TUI 仪表盘，以及 Python API 绑定。
4. **上游 LLM 服务**：真正处理模型请求的服务端，可以是不同模型提供方或兼容 OpenAI / Anthropic 协议的服务。

主要数据流有两条：

- **任务委托流**：用户把任务交给 Hermes，Hermes 通过 `ask_codex` 或 `ask_claude` 发起委托；请求经 Unix Socket 到达 Control Server，再写入对应 PTY 面板，最终把终端输出作为结果返回。
- **模型代理流**：Codex CLI、Claude Code 或其他客户端发起模型请求；Proxy API Server 接收后路由到上游 LLM 服务，并把请求摘要、响应预览和 Trace 事件交给 Manager Service。

Codex App 是一个独立的图形化入口。它由 AgentPlanFlow 统一启动、定位、隐藏和恢复，但不直接参与 Hermes 的 PTY 委托返回链路。这让人工交互和自动委托保持了清晰边界。

### 3.2 Rust Proxy Service 内部模块结构

下图展示 Proxy Service 内部主要 crate 和模块之间的关系。

![Proxy Service 内部模块依赖图](/assets/img/2026-05-16-AgentPlanFlow/proxy-modules.bordered.webp)

Proxy Service 由两类模块组成：

- **核心服务**：`main` 是程序入口；`api_server` 负责 API 代理，默认监听 `127.0.0.1:3001`；`manager` 负责 Console / Trace 管理，默认监听 `127.0.0.1:3002`；`web_app` 提供 Web 仪表盘，默认监听 `127.0.0.1:3000`。
- **支撑模块**：`plan` 承担任务拆解和协作相关能力；`clients` 封装上游模型客户端；`trace` 提供追踪基础设施；`python_api` 通过 PyO3 暴露 Python 扩展；`tui_app` 提供终端仪表盘；`vendor` 放置外部协议和执行策略相关依赖。

从图中可以看到，`api_server`、`manager` 和 `web_app` 构成了运行时的服务闭环：API Server 处理模型请求，Manager 聚合可观测数据，Web / TUI / Trace 面板再把这些数据展示出来。

---

## 四、核心工作流程

### 4.1 应用启动流程

下图展示从执行入口到桌面面板可用的启动过程。

![AgentPlanFlow 启动流程](/assets/img/2026-05-16-AgentPlanFlow/startup-flow.bordered.webp)

启动流程分为 6 步：

1. **检查 GTK 环境**：`maybe_reexec_with_system_python()` 检查 PyGObject、GTK3 和 VTE 是否可用，必要时切换到系统 Python 重新执行。
2. **加载配置文件**：读取 `config.yaml`、`model_config.yaml`、`api_key_config.yaml` 等配置，准备工作目录、模型和代理参数。
3. **构建桌面窗口**：创建 GTK3 主窗口，并组织 Codex App 嵌入区、Codex、Claude、Hermes、Trace 和状态栏布局。
4. **启动面板进程**：在独立 PTY 中启动 Codex、Claude、Hermes 等 CLI；同时启动 `codex-desktop`，等待其 X11 窗口被捕获后同步到嵌入区。
5. **启动控制服务**：创建 `cache/control.sock`，监听来自 Hermes Bridge Plugin 的跨面板请求。
6. **进入交互状态**：窗口恢复布局，Hermes 面板获得焦点，用户可以开始输入任务。

这条启动链路的关键点是先准备桌面运行环境，再启动智能体面板，最后开放跨面板控制。这样可以保证 Hermes 开始编排任务时，Codex / Claude 面板和本地 Socket 服务都已经就绪。

### 4.2 跨智能体协作流程

下图展示 Hermes 将子任务委托给 Codex 或 Claude 的链路。

![Hermes 委托子任务流程](/assets/img/2026-05-16-AgentPlanFlow/delegation-flow.bordered.webp)

以“先让 Codex 生成代码，再让 Claude 审查”为例，协作过程如下：

1. Hermes 理解用户任务，决定调用 `ask_codex` 或 `ask_claude`。
2. Bridge Plugin（`agentplanflow-bridge`）把委托内容封装成本地 JSON 请求。
3. 请求通过 Unix Domain Socket 发送给 Control Server。
4. Control Server 执行 `run_prompt` 动作，把 prompt 写入目标面板的 PTY stdin。
5. Codex 或 Claude 在原 CLI 会话中执行任务，并持续向 PTY stdout 输出结果。
6. Control Server 读取终端输出，并通过输出变化和 idle timeout 判断任务是否完成。
7. 任务结果沿 Socket 返回给 Bridge Plugin，再回到 Hermes 的上下文中。
8. Hermes 根据返回结果继续拆解任务、调用另一个智能体，或把最终结论交还给用户。

这条链路的特点是低侵入。AgentPlanFlow 不要求 Codex 或 Claude 暴露额外 API，而是复用已有 CLI 的认证、会话、模型配置和交互界面。代价是任务完成判断依赖终端输出和 idle timeout，因此长时间流式输出、强交互确认或输出格式变化，都需要更谨慎地配置超时和结果抓取策略。

### 4.3 API 请求代理流程

下图展示模型请求经过 Proxy Service 的转发过程。

![API 请求代理流程](/assets/img/2026-05-16-AgentPlanFlow/api-proxy-flow.bordered.webp)

代理链路如下：

1. Codex CLI、Claude Code 或其他客户端发起 HTTP 模型请求。
2. Proxy API Server（Rust Axum，默认端口 `3001`）接收请求。
3. API Server 根据路径进入 OpenAI 风格或 Anthropic 风格的兼容处理逻辑，例如 `/v2/chat/completions`、`/v1/*` 和 `/anthropic/v1/messages`。
4. Proxy Service 根据模型配置选择对应的上游客户端，并转发请求。
5. 上游 LLM 服务返回普通响应或 SSE 流式响应。
6. 响应沿原请求路径返回给调用方。
7. Manager Service 同步记录请求摘要、响应预览、Trace 事件和 Console 输出，供 Web 仪表盘、TUI 仪表盘和 Trace 面板查看。

这层代理的价值不只是“转发请求”。它把模型适配、协议兼容和运行观测集中到一个 Rust 服务里，让桌面层可以更专注于窗口、面板和任务编排。

---

## 五、设计问答

### 5.1 为什么选择桌面应用，而不是纯 Web 应用？

- **保留原生 CLI 体验**：Codex、Claude 和 Hermes 仍以各自熟悉的终端形态运行，用户可以随时查看、输入和干预。
- **复用本地开发环境**：工作目录、认证状态、终端配置和模型选择都可以跟随本机环境，不需要重新搬到浏览器沙箱中。
- **支持 Codex App 嵌入**：在 X11 环境下，Codex Desktop 可以被嵌入到 GTK 工作台中，形成更统一的桌面体验。
- **本地控制链路更短**：跨面板委托通过 Unix Socket 和 PTY 完成，适合单机桌面场景。

### 5.2 为什么同时保留 Codex App 和 Codex CLI？

Codex App 和 Codex CLI 在 AgentPlanFlow 中承担的是两种互补入口。

- **Codex App** 更适合用户主动探索、调整和继续对话，保留 Codex Desktop 自身的产品化体验。
- **Codex CLI 面板** 更适合被 Hermes 自动委托，因为它运行在可控 PTY 中，Control Server 可以写入 prompt、读取 stdout，并捕获任务结果。
- **嵌入方式低侵入**：AgentPlanFlow 不改造 Codex App 内部实现，只负责启动、定位、隐藏和尺寸同步，因此 Codex App 可以继续按原应用方式升级和运行。
- **工作流边界清晰**：人工探索走 Codex App，自动化子任务和跨面板结果回传走 Codex CLI PTY。

因此，两者并不是重复建设。Codex App 提供更完整的人工交互体验，Codex CLI 提供更适合编排的机器可控通道。

### 5.3 为什么用 Rust 编写 Proxy Service？

- **异步 I/O 友好**：Tokio 和 Axum 适合同时处理 API 请求、SSE 流、WebSocket 和管理端点。
- **可观测能力集中**：请求记录、Console 输出和 Trace 事件可以在同一个服务中组织。
- **工程边界清晰**：Python 负责桌面与进程编排，Rust 负责代理服务和高并发 I/O。
- **方便和 Python 互通**：通过 PyO3 暴露 Python API，桌面侧可以按需启动或集成 Rust 能力。

### 5.4 跨面板通信为什么基于 PTY？

跨面板通信可以简化为下面这条链路：

```text
Hermes
  | ask_codex("prompt")
  v
Bridge Plugin (Python)
  | JSON -> Unix Socket
  v
Control Server (Python)
  | PTY stdin -> 写入 prompt + 回车
  v
Codex Pane (PTY)
  | PTY stdout -> 返回结果
  v
Control Server -> Bridge Plugin -> Hermes
```

这种方式的好处是能直接复用 CLI 现有能力，不需要为每个智能体单独维护一套私有集成协议。它的成本也很明确：任务完成判断要依赖输出变化、idle timeout 和终端内容抓取，所以在长任务、持续流式输出或交互式确认场景下，需要把超时和回传策略配置得更保守。

---

## 六、实现边界与注意事项

AgentPlanFlow 采用低侵入方式整合现有 CLI，因此也继承了一些桌面与终端集成的边界：

- 跨面板委托依赖 PTY 输入输出和 idle timeout 判断任务完成，长任务、持续流式输出或交互式确认场景可能需要调高超时时间。
- Codex、Claude、Hermes CLI 的输出格式或 TUI 行为变化，可能影响结果抓取稳定性。
- Codex App 采用 X11 顶层窗口的视觉嵌入和位置同步，不等同于真正的 GTK 子控件嵌入；窗口管理器、显示缩放或 Wayland 环境都可能影响嵌入效果。
- Unix Socket 默认面向本机单用户场景，适合桌面工作台；如果扩展到远程或多用户场景，需要补充认证、授权和审计设计。
- `trace.enabled` 默认可以关闭；细粒度 Trace 记录需要结合配置启用，团队使用时也应明确日志保留和敏感信息处理策略。
- GTK3、VTE 和 X11 相关能力更适合 Linux 桌面环境，Wayland、macOS 或 Windows 的兼容性需要单独验证。

---

## 七、简单总结

AgentPlanFlow 展示了一种低侵入的多智能体编程工作台设计。它不要求重写 Codex、Claude 或 Hermes 的运行方式，而是通过桌面编排、PTY 控制和代理服务，把它们组织进同一个可观察、可委托、可继续人工干预的开发工作流。

它的核心价值可以概括为：

- **多智能体协作**：Codex 负责实现，Claude 负责审查，Hermes 负责任务拆解和编排。
- **Codex App 嵌入**：在 GTK 工作台内提供图形化 Codex 入口，同时保留 Codex CLI 作为可委托的自动化通道。
- **低侵入集成**：通过 PTY 和 Unix Socket 复用已有 CLI 会话，而不是强制接入专用 API。
- **可观测工作流**：通过 Manager、Trace、Console、Web 和 TUI 视图观察请求与任务执行过程。
- **本地桌面体验**：GTK3、VTE 和 Codex 桌面嵌入让多个智能体集中在一个窗口中工作。
- **可扩展代理层**：Rust Proxy Service 为模型兼容、请求记录和后续多模型适配提供基础。

整体来看，AgentPlanFlow 更像是一个本地多 Agent 编程实验平台。它把智能体协作从“多个终端之间手动切换”推进到“统一桌面中的可编排工作流”，也为后续探索自动拆解、审查闭环和多模型协同提供了清晰的工程基础。
