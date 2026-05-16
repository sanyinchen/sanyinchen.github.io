---
title: "AgentPlanFlow：多智能体协作编程的桌面工作台"
author: sanyinchen
date: 2025-05-16
categories: [ AI, 工作站 ]
tags: [AgentPlanFlow, AI-Agent, Codex, Claude, Hermes, 架构, 多智能体]
render_with_liquid: false
toc: true
---

## 一、项目概述

AgentPlanFlow 是一个面向多智能体协作编程的桌面工作台。它将 **Codex**（OpenAI）、**Claude Code**（Anthropic）和 **Hermes**（Nous Research）放在同一个原生桌面窗口中，让不同智能体可以在同一工作目录下独立运行、互相委托，并共享任务上下文。

除了传统的 Codex CLI 终端面板，AgentPlanFlow 还集成了 **Codex App / Codex Desktop**。它不是简单地再打开一个外部窗口，而是由桌面层启动 `codex-desktop`，再通过 X11 窗口定位与同步机制把 Codex App 视觉嵌入到 GTK 工作台左侧区域。这样用户既可以保留 Codex App 更完整的产品化交互，也可以继续使用 Codex PTY 面板参与 Hermes 委托链路。

从工程实现上看，AgentPlanFlow 由两层能力组成：桌面编排层负责窗口、终端、进程和跨面板控制；Proxy Service（代理服务）负责模型 API 兼容、请求记录、控制台输出和 Trace 事件管理。两者配合后，开发者既可以直接操作每个智能体，也可以让 Hermes 通过工具调用把任务分发给 Codex 或 Claude；完成后的结果还可以在 Codex App 中继续微调。

![agent_pannel.png](../assets/img/2026-05-16-AgentPlanFlow/agent_pannel.png)

三个智能体和辅助面板的职责如下：

| 面板 | 运行对象 | 主要职责 |
|------|----------|----------|
| **Codex App** | OpenAI Codex Desktop | 图形化 Codex 交互入口，视觉嵌入 GTK 工作台 |
| **Codex** | OpenAI Codex CLI | 代码生成、修复和局部实现，可被 Hermes 委托 |
| **Claude Code** | Anthropic Claude CLI | 代码审查、重构建议和复杂推理 |
| **Hermes** | Nous Research Hermes CLI | 任务编排，可通过 `ask_codex` / `ask_claude` 委托子任务 |
| **Trace** | 内置 Trace / Console 面板 | 展示代理请求、控制台输出和任务事件 |

这种设计的核心目标不是替代某一个智能体，而是把不同智能体的优势组织进同一个开发工作流中：Codex 更适合快速实现，Claude 更适合审查和解释，Hermes 则承担拆解与编排角色。

---

## 二、技术栈

### 核心技术

| 层次 | 技术选型 | 工程结构                                             |
|------|----------|--------------------------------------------------|
| 桌面 GUI | Python 3 + GTK3 + VTE | `index.py`、`app/`                                |
| Codex 桌面嵌入 | X11 窗口嵌入 | `app/embed/`                                     |
| 跨面板控制 | PTY + Unix Domain Socket + JSON | `app/control_server.py`、`service/hermes/bridge/` |
| API 代理服务 | Rust + Tokio + Axum | `service/proxy/`                                 |
| Web 仪表盘 | Rust + Axum + SSE | `service/proxy/crates/web_app/`                  |
| 终端仪表盘 | Rust + Ratatui | `service/proxy/crates/tui_app/`                  |
| Python 绑定 | PyO3 | `service/proxy/crates/python_api/`               |
| 容器化 | Docker + Compose | `docker/`                                        |

### 核心依赖关系

- `index.py` 是 GTK3 桌面应用入口，负责创建窗口、加载配置、启动 Codex、Claude、Hermes 和 Trace 面板。
- `CodexEmbedder` 负责启动并管理 Codex App，把 `codex-desktop` 作为独立 X11 顶层窗口固定到 GTK 容器区域中。
- 每个智能体面板运行在独立 PTY 中，保留原 CLI 的认证、会话和交互体验。
- Hermes 通过 Bridge Plugin（`agentplanflow-bridge`）访问本机 Unix Socket，把任务发送给 Codex 或 Claude 面板。
- Rust Proxy Service 提供 OpenAI / Anthropic 风格的兼容路由，并将请求、响应和控制台输出推送到观测层。

---

## 三、系统架构

### 3.1 整体架构图

下图展示桌面层、控制层、代理层和上游模型服务之间的关系。

![AgentPlanFlow 整体架构图](/assets/img/2026-05-16-AgentPlanFlow/architecture-overview.png)

架构可以分为四层：

1. **桌面窗口层**：GTK3 窗口承载 Codex App、Codex、Claude、Hermes、Trace 等面板。Codex App 通过 X11 视觉嵌入固定在左侧区域，Codex / Claude / Hermes 则以 PTY 终端面板运行。
2. **Service App 层（Python）**：负责 PTY 进程管理、Unix Socket 控制服务、面板输出采集和桌面状态维护。
3. **Proxy Service 层（Rust）**：提供 API 代理、请求记录、Trace 事件服务、Web 仪表盘、TUI 仪表盘和 Python API 绑定。
4. **上游 LLM 服务**：Codex、Claude 或其他客户端发出的模型请求，经由代理服务转发到配置的上游模型服务。

主要数据流如下：

- 用户输入进入 Hermes 面板，由 Hermes 决定是否拆解任务。
- Hermes 调用 `ask_codex` 或 `ask_claude` 后，请求经 Unix Socket 到达 Control Server，再写入对应 PTY。
- 用户也可以直接在 Codex App 中进行图形化 Codex 会话；该窗口由 AgentPlanFlow 统一启动、定位、隐藏和恢复，但不参与 Hermes 的 PTY 委托返回链路。
- Codex / Claude 发起模型请求时，Proxy API Server 接收并转发到上游 LLM 服务。
- Manager Service 收集请求记录、Trace 事件和 Console 输出，供 Trace 面板、Web 仪表盘或 TUI 仪表盘展示。

### 3.2 Rust Proxy Service 内部模块结构

下图展示 Proxy Service 内部主要 crate 和模块之间的关系。

![Proxy Service 内部模块依赖图](/assets/img/2026-05-16-AgentPlanFlow/proxy-modules.png)

核心模块包括：

- `main.rs`：程序入口，解析 `serve` / `tui` 等子命令。
- `api_server`：API 代理服务，默认监听 `127.0.0.1:3001`，覆盖 OpenAI 风格路径和 Anthropic 风格路径。
- `manager`：Console / Trace 管理服务，默认监听 `127.0.0.1:3002`，提供 `/console/std`、`/ws/console/std` 和 `/trace/events`。
- `web_app`：Web 仪表盘，默认监听 `127.0.0.1:3000`。
- `plan`：任务拆解与协作相关能力，包含任务类型、消息组装、工具注册和 review prompt 等。
- `clients`：上游模型客户端适配器。
- `python_api`：基于 PyO3 导出的 Python 扩展模块。
- `tui_app`：基于 Ratatui 的终端仪表盘。
- `trace`：追踪基础设施，包括宏和事件工具。
- `vendor`：外部协议和执行策略相关依赖。

---

## 四、核心工作流程

### 4.1 应用启动流程

下图展示从执行入口到桌面面板就绪的主要步骤。

![AgentPlanFlow 启动流程](/assets/img/2026-05-16-AgentPlanFlow/startup-flow.png)

启动过程分为 6 个步骤：

1. **检查 GTK 环境**：`maybe_reexec_with_system_python()` 确认 PyGObject、GTK3 和 VTE 可用，必要时切换到系统 Python 重新执行。
2. **加载配置文件**：读取 `config/app/config.yaml`、`config/models/model_config.yaml`、`config/models/api_key_config.yaml` 等配置。
3. **构建桌面窗口**：创建 GTK3 窗口，组织 Codex App 嵌入区、Codex、Claude、Hermes、Trace 和状态栏区域。
4. **启动面板进程**：在独立 PTY 中启动对应 CLI 或代理服务，并为面板注入工作目录、模型和代理配置；同时启动 `codex-desktop` 并等待其 X11 窗口被捕获后固定到嵌入区。
5. **启动控制服务**：创建 `cache/control.sock`，监听来自 Hermes Bridge Plugin 的跨面板请求。
6. **进入交互状态**：窗口恢复布局，Hermes 面板获得焦点，用户可以开始输入任务。

### 4.2 跨智能体协作流程

下图展示 Hermes 将子任务委托给 Codex 或 Claude 的链路。

![Hermes 委托子任务流程](/assets/img/2026-05-16-AgentPlanFlow/delegation-flow.png)

以“先让 Codex 生成代码，再让 Claude 审查”为例，协作流程如下：

1. Hermes 理解用户任务，决定调用 `ask_codex`。
2. Bridge Plugin（`agentplanflow-bridge`）构造本地 JSON 请求。
3. 请求通过 Unix Domain Socket 发送给 Control Server。
4. Control Server 执行 `run_prompt` 动作，把 prompt 写入 Codex 面板的 PTY。
5. Codex 在原 CLI 会话中执行任务，并将输出写回终端。
6. Control Server 通过输出变化和 idle timeout 判断任务是否完成，再读取增量输出。
7. 结果沿 Socket 返回给 Bridge Plugin，最终交给 Hermes。
8. Hermes 根据结果继续调用 `ask_claude`，完成代码审查或后续修复。

这条链路的特点是低侵入：它不要求 Codex 或 Claude 暴露额外 API，而是复用已有 CLI 的认证、会话、模型配置和交互界面。

### 4.3 API 请求代理流程

下图展示模型请求经过 Proxy Service 的转发过程。

![API 请求代理流程](/assets/img/2026-05-16-AgentPlanFlow/api-proxy-flow.png)

代理链路如下：

1. Codex、Claude 或其他客户端发起模型请求。
2. Proxy API Server（Rust Axum，默认端口 `3001`）接收请求。
3. 服务根据路径进入 OpenAI 风格或 Anthropic 风格的兼容处理逻辑。例如主路径覆盖 `/v2/chat/completions`，同时保留 `/v1/*` 兼容路由，并提供 `/anthropic/v1/messages`。
4. Proxy Service 选择配置的上游模型客户端并转发请求。
5. 上游 LLM 服务返回普通响应或 SSE 流式响应。
6. 响应沿原路径返回给调用方。
7. Manager Service 记录请求摘要、响应预览和 Trace 事件，供 Web / TUI / Trace 面板查看。

## 五、Q&A

### 5.1 为什么选择桌面应用而不是纯 Web 应用？

- **保留原生 CLI 体验**：Codex、Claude 和 Hermes 仍以各自熟悉的终端形态运行，用户可以直接查看和干预。
- **便于复用本地环境**：工作目录、认证状态、终端配置和模型选择都可以跟随本机开发环境。
- **支持 Codex 桌面嵌入**：在 X11 环境下，Codex 桌面窗口可以被嵌入到 GTK 容器中，形成更统一的工作台体验。
- **本地控制链路更短**：跨面板委托通过 Unix Socket 和 PTY 完成，适合单机桌面场景。

### 5.2 为什么同时保留 Codex App 和 Codex CLI？

Codex App 和 Codex CLI 在 AgentPlanFlow 中承担的是两种互补入口，而不是重复能力：

- **Codex App** 更适合作为用户直接操作的图形化工作区，保留 Codex Desktop 自身的产品体验、会话入口和交互细节。
- **Codex CLI 面板** 更适合被 Hermes 自动委托，因为它运行在可控 PTY 中，Control Server 可以稳定写入 prompt、读取 stdout 并捕获任务结果。
- **嵌入方式保持低侵入**：AgentPlanFlow 不改造 Codex App 内部实现，只负责启动、定位、置顶/隐藏和尺寸同步，因此 Codex App 可以继续按照原应用方式升级和运行。
- **工作流边界清晰**：用户主动探索可以走 Codex App；自动化子任务、跨面板委托和结果回传则走 Codex CLI PTY。

因此，Codex App 提供更完整的人工交互体验，Codex CLI 则提供更适合编排的机器可控通道。两者同时存在，正是 AgentPlanFlow 区别于“多开几个终端”的关键桌面体验。

### 5.3 为什么用 Rust 编写 Proxy Service？

- **异步 I/O 友好**：Tokio 和 Axum 适合同时处理 API 请求、SSE 流、WebSocket 和管理端点。
- **可观测能力集中**：请求记录、Console 输出和 Trace 事件可以在同一个服务中组织。
- **边界清晰**：Python 负责桌面与进程编排，Rust 负责代理服务和高并发 I/O。
- **可与 Python 互通**：通过 PyO3 暴露 Python API，方便桌面侧按需启动或集成 Rust 能力。

### 5.4 跨面板通信方案

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

这是一种基于 PTY 输入输出复用的低侵入方案。它的优势是可以直接复用 CLI 现有能力；代价是任务完成判断需要依赖输出变化、idle timeout 和终端内容抓取，因此对长时间流式输出或强交互式确认场景需要更谨慎地配置超时时间。

---

## 六、实现边界与注意事项

AgentPlanFlow 采用低侵入方式整合现有 CLI，因此也继承了一些桌面与终端集成的边界：

- 跨面板委托依赖 PTY 输入输出和 idle timeout 判断任务完成，长任务、持续流式输出或交互式确认场景可能需要调高超时时间。
- Codex、Claude、Hermes CLI 的输出格式或 TUI 行为变化，可能影响结果抓取稳定性。
- Codex App 采用 X11 顶层窗口的视觉嵌入和位置同步，不等同于真正的 GTK 子控件嵌入；窗口管理器、显示缩放或 Wayland 环境都可能影响嵌入效果。
- Unix Socket 默认面向本机单用户场景，适合桌面工作台；如果扩展到远程或多用户场景，需要补充认证、授权和审计设计。
- `trace.enabled` 默认可以关闭，细粒度 Trace 记录需要结合配置启用；团队使用时也应明确日志保留和敏感信息处理策略。
- GTK3、VTE 和 X11 相关能力更适合 Linux 桌面环境，Wayland、macOS 或 Windows 的兼容性需要单独验证。

---

## 七、部署方式

### 本地开发

```bash
# 启动 GTK 桌面应用
python3 index.py

# 或仅启动代理服务
cd service/proxy
cargo run -- serve
```

### Docker 开发环境

```bash
cd docker
./build-image.sh
docker compose up -d
docker compose exec dev /bin/bash

# 在容器内启动桌面应用
python3 index.py
```

---

## 八、总结

AgentPlanFlow 展示了一种低侵入的多智能体编程工作台设计：它不要求重写 Codex、Claude 或 Hermes 的运行方式，而是通过桌面编排、PTY 控制和代理服务，把它们组织进同一个工作流。

它的核心价值可以概括为：

- **多智能体协作**：Codex 负责实现，Claude 负责审查，Hermes 负责任务拆解和编排。
- **Codex App 嵌入**：在 GTK 工作台内提供图形化 Codex 入口，同时保留 Codex CLI 作为可委托的自动化通道。
- **低侵入集成**：通过 PTY 和 Unix Socket 复用已有 CLI 会话，而不是强制接入专用 API。
- **可观测工作流**：通过 Manager、Trace、Console、Web 和 TUI 视图观察请求与任务执行过程。
- **本地桌面体验**：GTK3、VTE 和 Codex 桌面嵌入让多个智能体集中在一个窗口中工作。
- **可扩展代理层**：Rust Proxy Service 为模型兼容、请求记录和后续多模型适配提供基础。

整体来看，AgentPlanFlow 更像是一个本地多 Agent 编程实验平台：它把智能体协作从“多个终端之间手动切换”推进到“统一桌面中的可编排工作流”，也为后续探索自动拆解、审查闭环和多模型协同提供了清晰的工程基础。
