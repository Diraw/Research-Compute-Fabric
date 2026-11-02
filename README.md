# Research Compute Fabric (RCF)

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**给 AI Agent 用的科研算力控制面：先计划、再确认、可追溯。**

RCF 把本机、SSH 工作站（含 WSL）和 Slurm 集群收进同一套 CLI 与 stdio MCP 接口。它不取代调度器，
也不要求远端常驻服务；配置用 TOML，计划、运行、GPU 租约与审计用 SQLite。

```text
Agent / CLI → RcfService → 配置、计划、SQLite、审计
                            ├─ Local 进程
                            ├─ SSH + 状态文件
                            └─ Slurm 客户端命令
```

## 为什么不是又一个 `sbatch` 包装器

提交一条命令很容易，难的是回答：

- 这次跑的是哪份代码、哪个环境、哪批输入？
- 命令会在哪台机器、哪个目录、用哪张 GPU 执行？
- 提交回执丢了，作业是没跑、在跑，还是已经结束？
- 产物在哪、能不能复现，旧文件能不能安全清理？

RCF 把这些答案放进同一条链路：**plan → review → submit → observe**。所有执行先有一份可审阅的
计划，计划里包含命令预览、工作目录、资源需求、环境指纹和阻塞项；提交必须显式确认；
运行状态从执行器重新读取，不拿数据库里的旧值冒充事实。

## 能做什么

| 能力 | 说明 |
| --- | --- |
| 节点登记与探测 | Local / SSH / Slurm；只读探测 CPU、内存、磁盘、GPU 与分区 |
| 计划与执行 | 先 `run plan` 审阅，再 `run submit --yes`；阻塞项存在时拒绝提交 |
| 运行观测 | 状态、日志、退出码、事件、结果清单；不确定提交保持 `unknown` |
| 环境证据 | 环境指纹、漂移、`env lock`，以及按路径所有权放行的安装计划 |
| 代码与数据 | `project sync` / `project push`，按内容差异传输并逐文件校验 |
| 存储治理 | 台账、扫描、两阶段清理；删除必须审阅具体文件清单并再次确认 |
| 进度与预算 | 项目写入 JSONL 证据；ETA 只来自实测推进，没有证据就返回不可估 |
| Agent 接入 | 29 个 MCP 工具；CLI 与 MCP 共用 `RcfService` 和同一套错误码 |

## 安装

需要 Python 3.11+。仓库带 `uv.lock`，推荐用 [uv](https://docs.astral.sh/uv/)：

```bash
cd research-compute-fabric
uv sync
uv run rcf --help
```

没有 uv 时也可以用虚拟环境：

```bash
python -m venv .venv
# Linux / macOS
.venv/bin/python -m pip install -e .
```

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
```

安装依赖可能需要网络，请只在你允许的环境中执行；RCF 不会自行下载依赖，也不会连接未登记的节点。
安装后可直接使用 `rcf`；如果没有激活虚拟环境，用 `uv run rcf` 或虚拟环境中的绝对路径。

## 5 分钟本机演示

```bash
uv run python examples/quickstart.py
```

脚本会在系统临时目录创建独立的 RCF home，用当前 Python 运行 `examples/demo/compute.py`，
执行一次 1000 步的 CPU 小任务，并核对确定性结果。它不连接远程节点，临时目录会保留供检查。

输出类似下面这样（路径和 ID 因机器而异）：

```text
Isolated RCF home retained at: .../rcf-example-...
Plan plan_...: ...
Run run_...: succeeded
Completed 1000 steps; result written to the declared output directory
Verified result: .../outputs/result.json
```

这证明本机链路可用，不代表远程集群、GPU、MPI 或你的项目环境已经验证。

## 配置与第一次真实运行

```bash
uv run rcf init        # 生成 ~/.rcf/{rcf,nodes,projects,environments}.toml
uv run rcf doctor      # 检查配置、依赖与安全姿态
```

按 `examples/` 修改自己的 RCF home。示例里的 `/home/researcher`、`/path/to/checkout` 和
`example-*` 都是占位符，必须替换；不要把实际配置提交到仓库。

`--home` 是全局参数，要放在子命令之前：

```bash
rcf --home /path/to/private-rcf-home doctor
```

一次运行的最小闭环：

```bash
rcf run plan --project demo --node local \
  --param steps=1000 --param seed=1 --minutes 1
rcf run submit PLAN_ID --yes
rcf run status RUN_ID
rcf run logs RUN_ID
rcf run results RUN_ID
```

`PLAN_ID` / `RUN_ID` 用上一步实际返回值替换，不是可以直接执行的字面参数。`submit` 只接受
`plan_id`，不接受临时拼的命令行；计划默认 30 分钟后过期，过期后应重新 plan。

接入新机器前，先手动 `ssh` 一次并核对主机指纹，再运行：

```bash
rcf node inspect NODE
rcf node selftest NODE   # 约 10 秒，只验证控制路径与目录，不提交作业
```

## 接入 Agent（MCP）

`rcf mcp serve`（等价 `rcf-mcp`）启动 stdio MCP server。当前暴露 29 个工具，覆盖节点探测、
计划/提交/状态/日志、环境、项目同步、结果回收、存储治理和进度/ETA。

Claude Code 或其他兼容 stdio 的客户端示例：

```json
{
  "mcpServers": {
    "rcf": {
      "command": "rcf",
      "args": ["mcp", "serve"],
      "env": { "RCF_HOME": "/home/you/.rcf" }
    }
  }
}
```

Windows 把 `RCF_HOME` 写成 `C:/Users/you/.rcf`；如果 `rcf` 不在 `PATH`，使用虚拟环境中的
绝对路径。`rcf mcp tools` 可查看当前工具清单，完整配置、工具表和 Agent 工作循环见
[Agent 接入指南](docs/agent-guide.md)。

## 安全边界（先读这一段）

RCF **不是沙箱**。任务以当前账户权限运行；危险命令检测、日志脱敏都是启发式，不是安全隔离。

已经强制的约束：

- 计划与执行分离：`plan_run` 无副作用，`submit_run` 需要 `confirm` 且计划中没有阻塞项；
- 配置中没有密码、私钥或 token 字段，SSH 主机密钥验证永不放宽；
- 环境写入按路径所有权与显式确认放行，`[environments].managed_roots` 默认为空；
- 有副作用的操作写审计；RCF 内部 GPU 租约用 SQLite 部分唯一索引避免重复占用。

明确不保证：多用户隔离、恶意脚本防护、远程文件防篡改、全局 GPU 调度、跨机断点续传、
自动备份/清理，以及多节点 MPI 的普适兼容。完整清单见[安全模型与边界](docs/security.md)
和[验收状态](docs/status.md)。

## 项目状态

当前版本 `0.1.0`，处于公开候选阶段：离线回归与模拟执行器已有覆盖；原生 Linux/macOS、
远端 CI 和真实计算节点尚未在本轮公开验收中声明通过。能力、测试入口和已知限制以
[`docs/status.md`](docs/status.md) 为唯一可信来源。

## 文档

| 文档 | 内容 |
| --- | --- |
| [文档索引](docs/README.md) | 按角色选择阅读路径 |
| [Agent 接入指南](docs/agent-guide.md) | CLI/MCP、配置、进度与存储工作流 |
| [架构](docs/architecture.md) | 组件、模型、状态机与关键流程 |
| [安全边界](docs/security.md) | 已实现约束与明确不保证的内容 |
| [验收状态](docs/status.md) | 公开版本的验证口径与待验收项 |
| [开发规范](docs/development.md) | 离线测试、平台约定与构建 |
| [发布清单](docs/publication.md) | 隐私检查、源码交付与发布边界 |
| [技术决策](docs/research.md) | 依赖选择与取舍 |
| [HPC 应用兼容性](docs/hpc-lammps.md) | LAMMPS / Python / MPI 检查方法 |
| [路线图](docs/roadmap.md) | 未覆盖场景与后续工作 |

## 仓库结构

```text
src/rcf/       服务层、执行器、运行管理、进度证据、存储治理
docs/          深入文档（从 docs/README.md 进入）
examples/      本机快速开始、示例配置与 CPU demo
tests/         离线/模拟执行器测试；真实设备测试默认不启用
tools/         公开材料扫描与发布候选验收
```

开发前请读[开发规范](docs/development.md)。测试默认不连接真实节点；真实设备验证需要单独授权。

## 许可证

MIT，见 [LICENSE](LICENSE)。
