# 总体架构

本文面向要修改代码或评审实现的人，描述当前分层、关键契约与状态流。使用方式、配置和
Agent 工作循环见 [Agent 接入指南](agent-guide.md)；验证范围见 [验收状态](status.md)。

## 0. 当前关键契约

- CLI/MCP 共用的 `inspect_environment` 只探测；任意 `verify_command` 由独立 execute 入口
  `verify_environment(confirm=true)` 执行并审计。当前 MCP 共 31 个工具，清单以 `TOOL_SPECS` 为准。
- 内部模板值为保留名，`RunRequest.params` 与规划组装双重拒绝覆盖；环境变更除词法所有权外还需
  目标端祖先路径证据，不能通过 symlink/junction 或未知存在性越界。
- local 节点与 controller 共享物理 guard identity；同 SSH 连接配置也共享 endpoint identity；
  保留策略引用真实 owner 和全部相关结果根，
  当前输入声明不依赖历史 run 才生效。
- push 逐个安全解包为新普通文件，不沿已有目录链接写入，也不原地覆写共享硬链接 inode。
  POSIX 用目录 fd + replace；Windows 严格 pin 阻止 reparse 写入，旧文件备份后排他创建新 inode，
  不承诺 Windows 单文件原子性；失败保留恢复证据。
- Windows 短命令在暂停创建时绑定 Job Object，文件式 stdio 使 timeout 不依赖管道 EOF；正常返回
  保留 detached 作业。pilot 使用独立整树监督，父退出不代表执行树结束。
- 校准证据版本 2 绑定 resolved total/contract fingerprint；推进 checkpoint
  而非 heartbeat 相邻样本决定速率。
- 普通 SSH 的取消身份依赖 `/proc` 启动实例与打开脚本；缺失证据会拒绝信号。macOS 等无该接口
  的节点暂不支持自动证明取消，详见安全边界。

## 1. 分层

```
┌──────────────────────────────────────────────────────────────┐
│ 入口层        rcf CLI (Typer)          MCP server (stdio)     │
│               ↑ 同一份 JSON 契约，同一份错误码                 │
├──────────────────────────────────────────────────────────────┤
│ 服务层        RcfService（唯一的业务门面）                     │
│               list_nodes / inspect_node / plan_run / submit … │
├──────────────────────────────────────────────────────────────┤
│ 领域层  Registry  Resources  Environments  Projects  Runs     │
│         配置校验   探测/解析  三态+计划     路径映射   计划/执行 │
├──────────────────────────────────────────────────────────────┤
│ 执行层  Executor 协议：submit / status / logs / cancel        │
│         LocalExecutor   SshExecutor   SlurmExecutor           │
├──────────────────────────────────────────────────────────────┤
│ 基础设施  Runner（唯一进程出口）  Store（SQLite）  Policy（权限）│
└──────────────────────────────────────────────────────────────┘
```

依赖方向严格向下。`core/` 不 import `executors/`，`executors/` 不 import `runs/`，
`service.py` 是唯一把各层拼起来的地方。

## 2. 组件职责

| 模块 | 职责 | 关键约束 |
| --- | --- | --- |
| `core/config.py` | 读取 `~/.rcf/*.toml`，校验、合并 `*.d/`、交叉引用检查 | 未知字段直接报错；凭据字段不存在 |
| `core/models.py` | `*Spec`（声明式，`extra=forbid`）与运行时模型（`extra=ignore`） | 配置拼错必须失败，数据库向后兼容必须容忍 |
| `core/runner.py` | 所有外部进程的唯一出口；`CommandResult` 统一结果 | 业务模块不得直接 `subprocess` |
| `core/nodeops.py` | 节点小查询：目录存在、git 状态、可写性、磁盘 | 本地用 stdlib，远程一次 SSH |
| `resources/probes.py` | 一次批量探测脚本 + 本地探测 | 缺工具只记 `Check`，不抛异常 |
| `resources/parsers.py` | 纯函数解析 `/proc`、`df`、`nvidia-smi`、Slurm | 可离线测试，容忍缺列 |
| `resources/inspector.py` | 快照缓存、GPU 可调度性、容量检查 | 显存空闲 ≠ 可用 |
| `executors/*` | 提交/查询/取消/日志 | 状态必须能从磁盘或调度器重建 |
| `environments/manager.py` | 只读探测 + 安装计划 | 只允许改环境私有前缀 |
| `projects/service.py` | 项目视图 + git 同步计划 | 脏工作区拒绝切换 |
| `runs/planner.py` | 选节点、选 GPU、渲染命令、生成检查项 | 纯函数式：不产生副作用 |
| `runs/manager.py` | 提交、轮询、取消、结果、事件、租约 | 幂等 + 终态释放租约 |
| `runs/store.py` | SQLite（WAL）+ 唯一约束 | 单用户控制面，不做多写服务器 |
| `progress/planning.py` | 进度契约门禁、负载 key、试跑与预算检查 | 无实测证据不给正式预算放行 |
| `progress/collector.py` | 有界读取 JSONL 尾部（本地/远端 helper） | 不跟随链接；截断是覆盖度，不是数据可用性 |
| `progress/estimator.py` | 纯函数：推进速率、ETA 区间、停滞/预算告警 | 心跳不等于推进；无证据就返回不可估 |
| `progress/manager.py` | 观察、修订缓存、校准、有界等待 | deadline 共享；终态才校准；零等待不探测 |
| `progress/pilot.py` | 生成零依赖的限时 watchdog | 1..300 秒硬截止；不包装正式任务 |
| `storage/fs.py` | 有界 JSON 扫描、候选哈希校验、根约束删除 | 不跟随链接/挂载点；证据不足即拒绝 |
| `storage/ledger.py` | 台账、计划、复制回执、资源守卫、journal | 原子认领；短事务内不执行网络操作 |
| `storage/manager.py` | 统计、归属、保护判断、计划/执行/恢复 | 未知归属默认保护；扫描不等于删除授权 |
| `security/policy.py` | 权限分级、SSH 加固、危险命令拦截 | 未知操作默认拒绝 |
| `service.py` | 门面：JSON 化、错误归一 | CLI 与 MCP 必须等价 |

## 3. 数据模型

### 3.1 声明式（TOML；只有脱敏示例可公开）

| 文件 | 内容 | 不允许出现 |
| --- | --- | --- |
| `rcf.toml` | `[security]`、`[database]`、`[defaults]` | 任何路径以外的密钥 |
| `nodes.toml` | `[nodes.<id>]`：kind、ssh_alias、work_root、labels、slurm 块 | 私钥、密码、token |
| `projects.toml` | `[projects.<id>]`：入口模板、每节点路径、数据路径、env_id | 绝对凭据 |
| `environments.toml` | `[environments.<id>]`：kind、python、path、packages | 系统级修改指令 |

`*Spec` 模型 `extra="forbid"`：多写一个键就报错，避免「写了但没生效」的静默失败。

### 3.2 动态（SQLite，`state/rcf.db`）

| 表 | 用途 | 关键约束 |
| --- | --- | --- |
| `plans` | 计划快照（可审阅、可复现） | 主键 `plan_id` |
| `runs` | 任务记录（含 handle） | `idempotency_key` 唯一（部分索引） |
| `run_events` | 状态变化与失败原因的追加日志 | 按 `id` 排序 |
| `gpu_leases` | 平台内 GPU 预约 | `(node_id, gpu_key) WHERE released_at IS NULL` 唯一 |
| `audits` | 有副作用操作的审计 | 操作 + 层级 + 决策 |
| `environment_verifications` | `verified` 状态的证据 | `(env, node, fingerprint)` 唯一 |
| `storage_snapshots` | 有界扫描快照（完整性/时效/分类统计） | 按 node+root 取最新 |
| `storage_owned` | 文件归属与最近指纹 | 未登记文件不在此表 |
| `storage_plans` | 清理/登记计划与会话状态 | 具体路径，不接受 glob |
| `storage_copies` | 逐文件复制回执 | 只有 `complete && verified` 可作副本证据 |
| `storage_pins` | run 保留标记 | 变更提升保护版本 |
| `storage_guards` | 路径资源守卫 | 祖先重叠互斥；中断不自动 TTL 释放 |
| `storage_journal` | 清理逐项状态 | 用于中断恢复与审计 |
| `progress_observations` | 进度观察历史（每 run 最多 128 条修订） | 只存 JSON，不存原始日志 |
| `progress_latest` | 每个 run 的最新观察与 revision | 轮询可缓存，不重复探测 |
| `progress_calibrations` | 按负载 key 的实测耗时证据 | 只有终态、真实推进、未截断的样本 |
| `schema_meta` | schema 版本 | — |

`gpu_leases` 的部分唯一索引是并发协调的**唯一**真相：两个 Agent 同时计划同一张卡，
第二个一定在 `INSERT` 时拿到 `IntegrityError`，RCF 把它翻译成 `conflict` 错误并附上持有者。

## 4. 执行器契约

```python
class Executor(Protocol):
    kind: str
    node_id: str
    def submit(self, spec: JobSpec) -> SubmissionReceipt: ...
    def status(self, handle: JobHandle) -> JobStatus: ...
    def logs(self, handle: JobHandle, *, tail: int) -> str: ...
    def cancel(self, handle: JobHandle) -> JobStatus: ...
```

`JobHandle` 必须**自足**：SSH/Slurm 的 handle 里带 `workdir` 与 `log_path`，
本地执行器带 `run_dir`。控制进程退出、SSH 断开、数据库重启之后，只看 handle 就能继续轮询。

### 4.1 四个实现的落点

| | 启动方式 | 运行位置 | 状态来源 | 取消方式 |
| --- | --- | --- | --- | --- |
| Local | 分离的 supervisor 进程（新会话） | 本机 | `pid` + `exit_code` 文件 | 进程组 SIGTERM→SIGKILL / `taskkill /T` |
| SSH | `setsid bash run.sh`（无 `setsid` 用 `nohup`） | 远程普通目录 | 同一组文件 + `pid` 存活性 | 进程组信号 |
| Slurm | `sbatch --parsable` | 调度器分配的计算节点 | 先看 `exit_code`，再 `sacct`，再 `squeue` | `scancel` |
| Windows task | Task Scheduler → `cmd.exe` → `wsl.exe` → `bash rcf-task.sh` | Windows 上的 WSL2 | 同一组文件；计划任务状态用于区分“尚未写 pid” | WSL 进程组信号 + `Stop-ScheduledTask` |

四种执行器写**同一套状态文件**，这是「SSH 断线可恢复」的实现基础：

```
<run_dir>/
  run.json      元数据（项目、参数、环境指纹、命令）
  params.json   参数（供程序读取）
  run.sh        SSH / Windows task / Slurm 共用
  rcf-task.sh   Windows task 的 bash 启动器
  rcf-task.cmd  Windows task 的 cmd 启动器（Task Scheduler 直接运行它）
  rcf-task.log  Windows task 启动器日志（UTF-16LE 时由读取层解码）
  pid           脚本 pid
  started_at / finished_at   UTC ISO-8601
  exit_code     原子写入（写 .tmp 再 mv）
  run.log       命令的 stdout+stderr
  env.log       环境准备输出（module load / conda activate）
  progress.jsonl 进度证据（契约启用时；单写者、追加、每行一个 JSON）
  rcf-pilot.py / pilot-args.json / pilot.json  显式试跑的硬 watchdog 与结果
```

Slurm 的 `#SBATCH --output` 指向 `slurm-%j.out`（调度器自己的消息），
业务日志固定在 `run.log`，两者不混。

**Windows task 的边界**：节点的传输/探测仍是 `kind = ssh`，只把长作业的启动方式
换成 Task Scheduler；`NodeSpec.executor_kind` 返回 `windows_task`，计划与 run 记录能看出
实际执行器。计划任务没有触发器，只在提交时手动启动，终态后删除；运行在用户登录会话，
不需要管理员。`cmd.exe` 包裹是必需的：在非控制台任务上下文里直接启动 `wsl.exe` 会以 -1
退出；`-d <distro>` 也不能加引号，否则当前 WSL 会报 `WSL_E_DISTRO_NOT_FOUND`。

### 4.2 状态机

```
planned ──submit──> submitting ──sbatch/nohup──> pending ──> running
                       │                            │           │
                       └── error ──> failed        └───────────┴──> succeeded / failed
                                                                    lost（进程消失且无退出码）
                                                                    cancelled（scancel / 信号）
```

`lost` 是显式状态：进程没了、退出码也没写出来（机器重启、OOM killer、手动 kill -9）。
它比谎报 `failed` 更诚实，Agent 可以据此决定是否重跑。

## 5. 关键流程

### 5.1 plan（无副作用）

```
RunRequest
  → 项目/环境校验
  → 候选节点（显式指定 > 声明了路径 + 标签匹配）
  → 逐个探测（最多 4 个）：快照 → 检查项
  → 选定节点：环境视图 → GPU 选择（含租约）→ 渲染命令
  → RunPlan{checks, command.preview, run_dir, result_dir, gpu_leases}
  → 写入 plans 表
```

`checks` 只有 `ok / warn / fail / unknown / skipped` 五种。**`fail` 即阻塞**，
submit 会拒绝并返回 `permission_denied`（附全部阻塞项），而不是「尽力尝试」。

### 5.2 submit（有副作用，需确认）

```text
幂等键 / plan 查重 → 已有 run 直接返回（含运行中、unknown 和终态）
     → 新提交检查有效期、阻塞项与确认
     → 复查计划中已有的 commit/dirty、环境指纹、输入清单 digest
     → BEGIN IMMEDIATE：认领 run + 预约 GPU + 写目录 recovery handle → COMMIT
     → 唯一认领者 executor.submit → 写正式 handle
     → 审计 + 事件 → 立即轮询一次
```

**同一 plan 至多 dispatch 一次**，不依赖调用方填写幂等键。SQLite 事务不包住远程命令，
两个控制进程同时提交时只有一个能认领；租约与 run 一起成功或一起回滚。
明确拒绝记录 `failed` 并释放租约；超时、连接断开、成功但缺失回执等不确定情况记录
`unknown`、保留恢复句柄与租约。原始异常仍向上传递；需要再次执行必须重新 plan，
但 unknown 必须先核查旧作业，不能靠新计划绕过不确定性。

### 5.3 observe

`status` 每次都是「重新问执行器」，而不是相信数据库里的旧值；只有变化时才写库和补事件。
网络失败不会把任务标成失败，而是写一条 `observe_failed` 事件并保留最后已知状态。

### 5.4 运行进度、ETA 与耗时预算

```text
项目进度契约（completion_condition + mode/total）
  → plan：解析总量、生成负载 key、检查试跑/预算证据（无证据即 fail）
  → 脚本用 reporter 追加 JSONL（phase/completed/total/elapsed/session/seq）
  → 有界采集尾部（字节/样本上限、不跟链接、远端只读 helper）
  → 纯函数估计：按 session/phase 隔离推进速率 → 中位速率 + 区间
  → 无上报=progress_missing；心跳超时=stale；推进超时=stalled；预算越界=告警（不杀作业）
  → 终态且样本完整 → 写入匹配负载的校准（含 evidence_at）
```

ETA 的唯一来源是**实测推进**。`time_minutes` 是调度器时限，`runtime_budget_seconds` 是计划与
告警预算，两者都不会被当成预计耗时。以下情况明确返回 `eta_seconds=null`：没有契约、没有
有效样本、只有心跳、正在启动宽限、session 重启、phase 切换、counter reset、收敛型任务、
证据截断到只剩心跳、或远端读取失败。

正式预算的证据门禁：

- 负载 key 覆盖项目/节点/参数/资源需求/命令覆盖/环境指纹/git commit 与 dirty/输入清单；
  `time_minutes` 被排除（它是上限，不是负载）。
- 校准只接受成功终态，或显式试跑被 watchdog 以 124 终止；必须单 session、单 phase、
  `total` 匹配、至少两次有效推进、速率比不超过 4 倍，且试跑确实消耗至少一半时限。
- 截断、损坏、早退、脏工作区、无 commit 都不产生可用校准；校准有有效期，重读旧运行不会续期。
- 正式任务超预算只写告警/阻止新计划，不自动取消；唯一会自动终止的是显式 `pilot_seconds`
  包裹的试跑（退出码 124）。

预算时钟优先使用 executor 写入 run 目录的 `started_at`（Slurm 排队时间不算运行时间），
终态用控制端 `finished_at` 冻结；文件缺失时 Slurm 明确 `budget_elapsed_unavailable`。
节点与控制端时钟不一致时，worker 起点由首个样本的 `timestamp - elapsed_seconds` 估计，
`clock_offset_seconds` 只归一化年龄与起点，并写 `clock_skew` 告警，避免把时钟差误报成停滞；
  reporter 重启/尾部截断后复用本 run 的历史偏移，取“当前估计/历史值”较大且不让最新样本落到未来者。
试跑 watchdog 从 planned argv 解析真实 Python 解释器（跳过 `mpirun`/`srun`），
因为部分计算节点没有裸 `python3`；POSIX 清理会先回收进程组 leader 再 KILL（macOS 对 zombie 组会 EPERM）。

`run progress` 默认先刷新一次运行状态，避免作业已结束却仍显示 cached running/stale；`run wait` 仍是有界长轮询：上限 `min(60, [progress].max_wait_seconds)`，共享同一个绝对 deadline，
`timeout_seconds=0` 只读缓存、不发起任何探测；超时返回 `timeout`，不伪造终态。

### 5.5 存储治理与清理

```text
explicit root → bounded scan（元数据） → 归属/引用/保护判断
     → plan：逐文件指纹 + 原因 + 保留副本 → 人工确认
     → controller OS lock → claim_plan（token/CAS） → 根资源守卫
     → target prepare（持久 generation/非阻塞 OS lock） → 重新校验指纹与引用
     → delete（持锁核对 generation）/登记 → journal + postcheck → 终态提交 → 释放守卫
```

- 只有 `storage_*` 表保存台账；不重写 run 历史。扫描记录 `complete` 与 `observed_at`，
  不完整或过期的快照不能授权删除。代码/数据扫描只读元数据；run 产物首次登记时补一次
  有界 SHA-256 基线，哈希失败的文件保持 unmanaged。
- 归属来自已验证的部署回执、带 RCF `run.json` 标记的 run 目录，或显式确认的历史登记。
  配置路径内未登记的历史文件仍是 `unmanaged`，只统计、不自动清理。
- controller OS lock 覆盖 claim 前到 postcheck 后；SQLite `BEGIN IMMEDIATE` + owner token/CAS
  阻止旧执行者回写。记录 controller/lock_scope/PID/process_instance/stage/heartbeat，
  PID/心跳不单独判活；外来控制端无法核验时拒绝接管。
- 根内 `.rcf-storage/operations` 的 generation/追加日志及 OS lock fence 迟到旧调用；
  同 generation 已完成返回原报告，crash 后同 generation 不重跑。目标 busy 时不得提升代次。
- 清理与 `run.submit`、`push`、`fetch` 共用路径资源守卫，按祖先关系判断重叠；
  删除失去回执时计划进入 `unknown` 并保留守卫，`BaseException`/状态写库失败也一样。
  `--resume` 先取得执行所有权并 fence 目标，再复核原文件清单。引用变化时
  `reconcile` 只读观察并终结旧计划（superseded），无删除，再允许新计划。
- 删除只接受具体普通文件与完整指纹；根外路径、符号链接/junction、目录、glob 和
  无法证明边界的平台均拒绝。逻辑字节与可回收物理空间分开报告。

提交前即保存 run 目录指针；SSH/Local 可从 PID/退出码恢复。Slurm 提交包装先将 job ID
原子写入远端 `job_id` 文件，再输出回执；失去 SSH 回复后 `status` 可读回该文件继续追踪。
目录存在但没有 PID/调度器 ID 不等于提交成功或失败，仍保留 `unknown`。
取消命令的返回码不得被吞掉；只有执行器提供终态证据才释放租约，不能把“没有 handle”
或“scancel 请求已发送”当作已取消。
SQLite 条件更新拒绝覆盖已经持久化的终态；终态写入与租约释放在同一事务内完成。
观察失败只更新错误元数据，不整条回写旧状态，避免较早的查询让已取消任务“复活”。

提交前复查只是现有证据范围内的漂移检测：dirty=true 后继续修改、同路径同大小输入替换、
复查后的竞态以及计算节点环境差异仍未隔离，不等于不可变实验快照。

### 5.6 发布验收

`tools/release_check.py` 用 tracked + nonignored untracked 的交付白名单生成独立源码快照，
排除运行数据与私人配置；sdist→wheel 构建后逐字对比全部运行时代码，再在 checkout 外 venv
离线无依赖安装 wheel、核对所有模块来源、CLI 空 home 与当前 MCP 工具映射。
实际私有 home、日志、数据库与运行证据不属于公开交付；示例与源码隐私审阅见 `publication.md`。
`.github/workflows/ci.yml` 定义三平台×两 Python 版本；本机验收与远端 CI 结果分开声明。

## 6. 扩展点

| 想加什么 | 改哪里 | 不需要改什么 |
| --- | --- | --- |
| 新执行器（如 PBS/LSF） | `executors/` 新增实现 + `factory.py` 一行 | 上层服务、CLI、MCP |
| 新资源指标（如 IB 网络） | `probes.py` 脚本 + `parsers.py` + 模型字段 | 执行器 |
| 新环境类型（如 Spack） | `environments/manager.py` 的 `interpreter()`/`plan()` | 运行管理 |
| 新存储后端/清理策略 | `storage/fs.py` 或 `storage/manager.py` | 执行器与服务门面 |
| Web API | 复用 `RcfService`，加 FastAPI 路由 | 核心逻辑 |

## 7. 工程约定

1. **进程出口只有一个**：`core/runner.py`。新增外部命令时，接受 `Runner` 参数，不要 import subprocess。
   （唯一例外：`LocalExecutor` 用 `spawn_detached`，因为它要的是「脱离父进程」而不是「执行并等待」。）
2. **错误即数据**：所有异常继承 `RcfError`，带 `code` / `hint` / `detail`；MCP 层转成
   `{"ok": false, "error": {...}}`，CLI 用它决定退出码（2 配置、4 不存在、5 连接、7 权限、8 冲突）。
3. **脱敏在出口**：`get_logs`、审计、错误详情都经过 `security/redaction.py`。
4. **测试替身不进入生产代码**：`ScriptedRunner` 放在 `core/runner.py` 是唯一例外（无副作用，供测试与演示）。
5. **模拟与真实分离**：`tests/fixtures/fake_ssh.py` 用真 bash 执行真命令，只替换「host」和「调度器」；
   真实集群测试打 `@pytest.mark.integration`，默认不跑。

## 8. 设计取舍

| 取舍 | 选择 | 原因 |
| --- | --- | --- |
| 一次批量探测 vs 多条独立命令 | 批量 | SSH 每次握手 0.3–1s，十条命令就是十秒 |
| 用 `sacct` 判定成功 vs 自己的 `exit_code` 文件 | 自己的文件优先，`sacct` 交叉验证 | 调度器数据库可能未启用/被清理 |
| 配置在 TOML vs 数据库 | TOML | 可进 git、可 diff、可 review；动态状态才进库 |
| GPU 预约在内存 vs DB | DB 唯一索引 | 跨进程（CLI 与 MCP 同时跑）也必须互斥 |
| 命令模板直接 `format` vs 先分词再替换 | 先 `shlex.split` 再逐 token 替换 | 参数含空格时不会悄悄变成两个参数 |
| 单用户 SQLite vs 服务端 | SQLite | Local-first；多用户 Web API 属于 roadmap |
