# RCF 文档索引

根目录的 [README](../README.md) 负责安装和 5 分钟演示；这里按「先做什么」组织深入文档。
所有状态类结论以 [status.md](status.md) 为准，不在其他文档里另立一套完成度口径。

## 按角色阅读

### 第一次评估这个项目

1. 根目录 [README](../README.md)：安装、本机演示、能力与边界。
2. [status.md](status.md)：当前版本到底验证了什么、哪些还没有。
3. [security.md](security.md) 第 2 节：明确不保证的部分。
4. [roadmap.md](roadmap.md)：还没做什么，以及下一步优先级。

### 让 Agent 接入并使用

1. [agent-guide.md](agent-guide.md) 第 1–3 节：节点准备、MCP 配置、工具清单。
2. 第 4 节：工作循环、环境、文件同步、存储治理、MPI、Windows、进度与预算。
3. 第 5 节：项目入口、占位符与产物契约。
4. 第 6 节：会被拒绝的操作，以及为什么这是设计如此。

### 修改代码或扩展执行器

1. [development.md](development.md)：先跑起来、三条硬规则、测试与发布约定。
2. [architecture.md](architecture.md)：分层、组件职责、状态机、扩展点。
3. [status.md](status.md) 第 2 节：每项能力对应的回归入口。
4. [publication.md](publication.md)：准备公开仓库时再做隐私与交付检查。

### 运维与真实设备接入

1. [security.md](security.md) 第 3 节：上线前检查清单。
2. [agent-guide.md](agent-guide.md) 第 1 节：节点侧依赖与 `node selftest`。
3. [hpc-lammps.md](hpc-lammps.md)：Python / MPI / LAMMPS 环境的排查方法。
4. [status.md](status.md)：真实设备验证的边界与当前声明。

## 文档地图

| 文档 | 回答什么问题 | 主要读者 |
| --- | --- | --- |
| [agent-guide.md](agent-guide.md) | 怎么把节点接进来、怎么让 Agent 安全地计划/提交/取结果 | 使用者、Agent 集成者 |
| [architecture.md](architecture.md) | 代码怎么分层、状态机与关键流程是什么、扩展点在哪 | 贡献者 |
| [security.md](security.md) | 哪些约束已经强制、哪些明确不保证、上线前检查什么 | 使用者、运维 |
| [status.md](status.md) | 当前版本验证了什么、哪些还没验证 | 所有人 |
| [development.md](development.md) | 本地怎么跑测试、改代码要守哪些规则、怎么构建 | 贡献者 |
| [publication.md](publication.md) | 公开前怎么扫描隐私、交付哪些文件、发布边界怎么定 | 维护者 |
| [research.md](research.md) | 为什么用系统 OpenSSH / Slurm CLI / SQLite，而不是自建控制面 | 评审者 |
| [hpc-lammps.md](hpc-lammps.md) | HPC 上 Python、MPI、LAMMPS 的兼容性怎么排查 | 使用者 |
| [roadmap.md](roadmap.md) | 未覆盖场景、优先级与非目标 | 所有人 |

## 维护约定

改动以下内容时同步对应文档，避免 README 与深入文档各说一套：

| 改了什么 | 必须同步 |
| --- | --- |
| MCP 工具、CLI 命令、配置字段 | `agent-guide.md`、根 `README.md` |
| 安全机制或明确不保证的边界 | `security.md` |
| 完成度、测试数字、验收范围 | `status.md`（唯一可信来源） |
| 分层、状态机、执行器契约 | `architecture.md` |
| 测试方式、构建与发布流程 | `development.md`、`publication.md` |

文档中的节点 ID、路径、SSH 别名和项目名一律用占位符；不要写入实际机器配置、凭据或科研数据。
