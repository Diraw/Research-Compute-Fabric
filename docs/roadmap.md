# 下一阶段开发计划

排序原则：**先用真实证据收口，再扩能力。** 每一阶段都以「可验证」为出口，不把未验证的
东西标成完成；「已有实现」只描述能力与回归入口，当前验收结果统一以
[验收状态](status.md) 为准。

## 0. 当前基线（2026-11-04）

- 已有跨 Slurm 集群、Linux 工作站、Windows/WSL 与 macOS 的真实运行样本；最长 433 秒。
- 真实路径已跑过：Slurm CPU、Slurm `gpu` 分区 probe、`srun`/`mpiexec`、SSH、WSL。
- 真实 run 最长 433 秒；没有长作业、排队或断线恢复样本。
- 从未真实执行：`run cancel`、`storage plan/apply`、进度校准；仓库没有 `integration` 测试。
- 复现元数据按适用 run 统计无丢失：plan 有 git 的 11 个 run、声明 inputs 的 4 个 run，
  全部写入 run 记录；其余 run 的项目本身不是 git 仓库或没有声明 inputs。

结论：能力广度已经够用，下一阶段主题是**路径覆盖与证据**，不是继续加功能。

## P0 · 收口当前工作树（进行中）

| 事项 | 产出 | 状态 |
| --- | --- | --- |
| 观测真值 | `state_observed_at` / `observed_age_seconds` / `stale`；成功探测清除旧 error | 已有实现，待合入 |
| 失败可诊断 | `get_run` / `run status` 顶层 `diagnosis` 摘要 | 2026-11-04 新增 |
| 产物契约噪音 | `command_override` 的 run 不再误报 `contract_violation`，改记 `output_contract_unchecked`；`none` 记 `output_contract_unmanaged` | 2026-11-04 新增 |
| 复现元数据 | plan 的 git/inputs 已完整传入 run；补 inputs 传递契约测试 | 2026-11-04 新增测试 |
| 环境安装入口 | MCP `compute_plan_environment` / `compute_apply_environment` | 已有实现，待合入 |
| 工作树收口 | 分块提交、全套离线测试、ruff、release_check | 2026-11-04 执行 |

出口：全套离线测试 + ruff + release_check 通过；`run status` 一条命令能看到失败原因；
新 run 在适用时 100% 带 git/inputs 元数据。

## P1 · 真实路径验收自动化（最高优先级）

目标：把手工跑过的真实路径固化成可重复的套件。没有这一层，任何重构都可能悄悄破坏
5 个异构节点的真实链路。

2026-11-04：`tests/integration/` 骨架已落地（默认 skip，`RCF_IT=1` 才启用）；只读 smoke
与隔离 home 的 60 秒本机全链路已通过；远端提交/cancel 模板已写好，默认 skip，待显式授权执行。

| 事项 | 产出 | 依赖 |
| --- | --- | --- |
| integration 骨架 | `tests/integration/` + `-m integration` + 环境变量 gate，默认 skip | 无 |
| 节点 smoke | 每个授权节点：selftest → env inspect → plan → 60 秒作业 → status → logs → manifest | 授权节点 |
| Slurm GPU 计算 | 在 Slurm `gpu` 分区跑一次真实 `--gres=gpu:1` 计算作业（不是 probe） | 账户 + 允许提交 |
| 真实取消 | 真机 `cancel`，核对 `scancel` 回执、终态证据与租约释放 | 授权节点 |
| 真实调度器夹具 | 脱敏保存 `sinfo/squeue/sacct` 输出，替换 `fake_slurm` 中与站点不符的字段 | 上两项 |
| 长作业/断线 | ≥30–60 分钟作业、排队、断开重连、`unknown` → 真实状态转换 | 授权节点 |
| 分区上限 | 计划阶段比对 `MaxTime`（处理 `infinite`）与分区容量 | 真实 `sinfo` |
| 连接复用 | 可选 `ControlMaster`/`ControlPersist`，默认关闭，显式开启 | 无 |

出口：授权网络下 `pytest -m integration` 全绿；`status.md` 从「不声明真实设备」改为
「已在真实 Slurm 集群与工作站验证」，附日期、版本与限制；取消与 GPU 分区各有真实审计记录。

## P2 · 三条规范的闭环

按用户最在意的三条规范组织，不按模块组织。

### P2.1 提交规范

- `params_schema`：plan 期校验参数类型/范围/必填，避免 `seed=abc` 到运行期才炸。
- 分区校验：`MaxTime`、CPU/内存容量、GPU `gres` 与请求数。
- `--tasks` 与 MPI 启动器一致性：单节点多进程、`srun`/`mpiexec` 的 communicator size 核对。
- 计划过期与幂等：已有实现，补真实设备回归。

### P2.2 文件规范

- run manifest：git commit、环境指纹、inputs sha256、命令、seed、outputs sha256，落盘
  `manifest.json`；现已在 run 记录中保存，待生成独立可交付文件。
- `rcf run fetch`：已有 tar + scp + SHA-256；待补断点重试/续传与大文件场景。
- 存储治理：`storage scan/report/check`、`project push` 逐文件回执、两阶段清理
  `storage adopt/plan/apply` 均已有实现；待补真实节点上的 ACL/硬链接/大文件/断网回执验证。
  永久删除始终需要逐次确认。
- `rcf run diff`：比较两次 run 的参数、环境、输入、产物差异。
- 可选：每日只读巡检 + 通知；自动删除保持关闭。
- 配额/项目存储系统对接：读取真实 quota/inode 数据，替代仅按目录统计。

### P2.3 环境规范

- 环境漂移接入 plan 的 FAIL 级检查（现在 inspect 可见，但未必阻断）。
- 用 `env plan/apply` 真机修复一次缺包（真实失败就是 `No module named`），记录审计。
- `env lock` / `env snapshot`（conda `--explicit`、`pip freeze`）在 3 个节点各生成一次，
  纳入 manifest。

出口：一个真实项目从 plan 到 fetch 全链路，manifest 完整可复算；环境漂移能在 plan 期阻断；
`storage plan/apply` 至少一次真实执行且回执可核对。

## P3 · 科研吞吐（按需，不预设日期）

| 事项 | 说明 |
| --- | --- |
| 参数扫描 | 一个 plan 展开 N 个独立 run（各自确认、不自动重试），统一收敛状态；面向多 seed / 多参数 |
| 长作业 | ≥1 小时、排队、日志增长、租约 TTL 与恢复 |
| 并发压力 | 2 个 MCP 会话 + 1 个 CLI 同时 submit，验证 SQLite 锁与租约一致性 |
| 使用统计 | 按项目/节点统计 GPU·小时、排队时间、成功率（只读报表，不做调度） |
| 进度与 ETA | 已有 JSONL 证据、不可估语义、试跑校准；待真实集群验证（退出 124、长时间排队、滚动稳定性）。框架回调与自适应总量按需再做 |

出口：3 组参数扫描 24 小时内完成，状态与产物可统一查看；并发提交无 lost update。

## 暂缓 / 非目标

以下项与既定边界冲突，除非用户明确改变边界，否则不投入：

- **GPU 队列 / 自动重试 / 后台清理**：Slurm 的调度交给 Slurm；非 Slurm 机器不排队。
- **预留 TTL 的等待队列**：当前「冲突即失败」，不引入排队语义。
- **Web Dashboard / 远程 API / 多用户**：单用户真实路径尚未验收完；先不做服务化。
- **跨机备份、Globus、对象存储**：交给专用工具。
- **多节点拆分同一任务**：非目标。
- **取代 Slurm / Kubernetes**：非目标。
- **在 HPC 上安装常驻 agent 或要求 root**：非目标。
- **自动穿透学校网络的隧道、自动部署项目依赖到系统 Python**：非目标。

## 建议的下一步（如果只做一件事）

**先合入 P0，然后在 Slurm `gpu` 分区跑一个真实的 60 秒 GPU 计算作业，并把它写成
integration 测试。** 这一次会把 GPU 请求、`gres` 解析、分区选择、环境激活、产物契约、
日志与状态观测一次性压到真实路径上，比继续增加工具数量更有价值。
