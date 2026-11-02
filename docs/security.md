# 安全模型与边界

RCF 是「给 Agent 用的执行平台」，所以安全目标不是「防住恶意用户」，而是：

1. **不让 Agent 因为误解而做出破坏性操作**；
2. **不让凭据进入 RCF 的数据面**；
3. **不让两个 Agent 同时以为自己在独占同一张 GPU**；
4. **所有有副作用的操作可追溯**。

它**不是**沙箱。下面把「已经强制」和「只是约定/未覆盖」分开写清楚。

## 1. 已强制的机制

### 1.1 不提供凭据配置字段

- 配置模型 `NodeSpec` 里**没有**密码/私钥字段，只有 `ssh_alias`（`core/models.py`）。
- SSH 连接由系统 `ssh` 完成，凭据来自 `~/.ssh/config`、ssh-agent、硬件密钥，而非 RCF 配置字段。
- `security/redaction.py` 在日志、审计、错误详情出口做兜底脱敏（`password=`、`token:`、
  `Bearer`、`AKIA…`、`ghp_…`、私钥块、`user:pass@host`）。

**限制**：这不等于凭据绝不进入数据面。用户命令、项目输出或错误消息仍可能包含秘密；
脱敏是启发式，如果程序使用未识别格式，日志、SQLite 或导出中仍可能泄漏。禁止公开实际 RCF home、
运行日志与审计库；发布前按 `docs/publication.md` 审阅工作树和历史。

### 1.2 主机密钥验证永不被削弱

- 加载配置时拒绝：`StrictHostKeyChecking=no|accept-new`、`UserKnownHostsFile=/dev/null`、
  `GlobalKnownHostsFile=/dev/null`、`CheckHostIP=no`、`BatchMode=no`、密码认证
  （`security/policy.py::normalize_ssh_options`）。
- 连接一律 `-o BatchMode=yes`（不会卡在交互式提示上），**不添加任何放宽主机密钥的参数**。
- 失败分类：`HostKeyRejected` / `AuthFailed` / `HostUnreachable` 三种错误给出不同的处置建议，
  其中 host key 错误会明确要求你「带外核对指纹后手动 `ssh` 一次」。

**限制**：首次连接的可信度取决于你 `~/.ssh` 的既有策略（通常是 TOFU）。RCF 不会替你判断指纹。

### 1.3 权限分级

`OPERATION_TIERS`（`security/policy.py`）把每个操作映射到四个层级：

| 层级 | 默认 | 例子 |
| --- | --- | --- |
| `read_only` | 开 | 探测节点、读日志、生成计划 |
| `execute` | 开（但需确认） | 提交任务、取消任务、git 同步、**在 managed root 里建环境** |
| `system_admin` | **关** | 修改系统环境、安装常驻服务 |

> 环境安装不再由布尔开关把关，而是由**路径所有权**把关，见下一节。

未登记的操作默认**拒绝**（`PermissionDenied`），避免「新加的功能忘了分级」。

环境 `verify_command` 是任意项目 shell 代码，不是只读探测。`environment.verify` 属于 EXECUTE，
必须 `confirm=true` / `env verify --yes`，不受关闭普通确认策略影响。MCP 的只读环境检查不暴露
verify 开关；兼容 API 的 verify 请求仍执行同一门禁。内部 run 占位符是保留参数，拒绝用户参数覆盖。

### 1.4 计划与执行分离

`plan_run` 不产生任何副作用（只写一条可查的计划记录）；`submit_run` 必须：

1. 拿到 `plan_id`（不接受临时命令）；
2. `confirm=true`（MCP）/ `--yes`（CLI）；
3. 计划里没有 `fail` 检查项。

计划里有完整的 `command.preview`，人能看懂将要执行什么。

### 1.5 危险命令拦截

`scan_dangerous_command` 命中即拒绝：`sudo`、`su - root`、`mkfs`、`dd of=/dev/`、`wipefs`、
`shred`、`rm -rf /`、`rm -rf ~`、`shutdown/reboot`、`systemctl stop/disable`、
`kill -9 1`、`chmod -R 777 /`、`crontab -r`、`autossh`、`ssh -R/-D`、`curl|bash`。

**限制**：这是正则黑名单，绕过方式很多（`python -c "os.system('rm -rf /')"`）。
它防的是「Agent 顺手写出的危险命令」，不是「蓄意攻击」。真正的边界是你给 Agent 的
项目脚本本身——那部分代码需要你自己 review。

### 1.6 环境：按「路径所有权」放行

旧模型是一个布尔开关（`allow_environment_mutation`）。它拦的是**动作**，而风险在**位置**：
同一个 `pip install`，专用用户前缀通常比共享 conda `base` 风险小，但都可能执行包安装代码、
改变依赖或影响实验。路径所有权不是无害保证。

现在的规则（`environments/ownership.py`，纯函数、有单测）：

1. 目标前缀必须落在 `rcf.toml` 声明的 `[environments].managed_roots` 之下；
   **默认为空 = 什么都不许动**。
2. 前缀**不存在** → 允许创建。
3. 前缀**已存在** → 只有两种情况允许写入：
   - 该环境声明了 `managed = true`（你显式交给 RCF 管），或
   - 这个前缀是 RCF 自己创建的（SQLite `managed_prefixes` 有记录）。
4. 无论如何都拒绝：`/usr`、`/bin`、`/lib*`、`/etc`、`/opt`、`/var`、`/public/software`、
   `C:\Windows`、`C:\Program Files`，以及任何含 `..` 的路径。
5. 每次变更仍需 `--yes`，并写入审计表；创建成功后记录到 `managed_prefixes`。
6. 词法所有权通过后还要在目标节点逐组件检查现存祖先，拒绝 symlink、Windows reparse/junction、
   非目录、无访问权与无法检查的状态；未创建的后缀也不豁免祖先检查。apply 与每个写步骤前
   重新检查。这仍不能证明磁盘安全，也无法锁住不遵循 RCF 协议的外部写者。

`system` / `module` / `apptainer` 类型仍然只读，计划直接 `blocked`。
所有命令里不出现 `sudo`（生成阶段就会被 `assert_safe_command` 拦下）。

### 1.7 不绕过调度器

- Slurm 节点只有 `sbatch/squeue/sacct/scancel`；代码里不存在「ssh 到计算节点执行」的路径。
- GPU 可用性在 Slurm 节点上标记为 `scheduler_granted`，不根据登录机的 `nvidia-smi` 判断。

### 1.8 GPU 并发协调

- 计划阶段查询 `gpu_leases`，被占用的卡会给出 `gpu_availability: fail` 并附持有者。
- 提交阶段用 `(node_id, gpu_key) WHERE released_at IS NULL` 的**部分唯一索引**原子预约；
  冲突返回 `conflict`。终态（成功/失败/取消）自动释放。

**限制**：租约只在 **RCF 内部**有效。别人在你机器上直接跑的程序、Slurm 自己分配的作业，
RCF 都管不到。所以：

- SSH/本机节点：租约 + 进程探测 + 显存占用三重判断，但仍然可能出现「我们以为是空的」；
- Slurm 节点：以调度器为准，RCF 不做二次预约。

### 1.9 审计

有副作用的操作写 `audits` 表：谁（`cli`/`mcp`）、什么操作、哪个层级、允许/拒绝、
节点、run_id、命令前 8 个参数、GPU 租约。`rcf db audit` 可查。

### 1.10 幂等与重复提交

- `idempotency_key` 唯一；同 key 不同工作内容 → `conflict`（不会静默复用别人的结果）。
- 同一 `plan_id` 重复提交 → 返回同一 run，运行中、unknown、终态均不再次 dispatch。
- SQLite `BEGIN IMMEDIATE` 将提交认领、run 记录与 GPU 租约写入同一事务；并发调用也只能有一个提交者。
- 新执行必须重新 plan；unknown 需先核查旧作业，不能盲目重跑。

### 1.11 不确定提交与取消

提交前持久化目录恢复句柄；连接失败或回执不确定时保留 `unknown` 与 GPU 租约。
Slurm 作业号在远端原子写入 `job_id` 后才返回，后续 `run status` 可恢复跟踪。
没有 PID/job ID/退出码只能说明无法确认；取消会返回冲突或非终态，不假报成功、不释放租约。
`scancel` 失败会结构化报错；调度器仍报告 pending/running 时继续跟踪。

普通 SSH 取消先核对持久化进程启动身份、boot 标识与打开的 run.sh 身份，再发送信号；
身份无法核实时保持 unknown，不会依据可复用 PID 直接终止。当前严格身份检查依赖 Linux `/proc`；
没有这些证据的 POSIX 主机（包括 macOS）会安全拒绝普通 SSH 取消，需人工核查，不能声称通用取消已验证。
传统信号仍非 pidfd 原子操作：身份复核到 kill 之间的极窄系统级竞态尚未消除；旧 run 缺实例凭据时也拒绝自动取消。

### 1.12 存储扫描、清理与永久删除

- 扫描/报告/计划是只读操作；`storage apply` 属于 EXECUTE，且**始终**要求本次确认，
  不受 `require_confirmation=false` 影响。清理计划绑定具体文件、大小、SHA-256、根身份与
  引用/保护版本；正常计划过期或证据变化即拒绝。中断计划只有显式 resume 并重新复核全部原清单时可超原有效期。
- 只有带归属证据**且带 SHA-256 基线**的文件可进入清理：已验证的部署回执、首次扫描时
  已成功哈希的 RCF run 产物，或显式确认的历史登记。历史代码只能用 `--from` 登记，
  且目标必须与源 checkout 逐文件 SHA-256 一致；远端独有修改不会被认领。无内容指纹的归属只统计、不可删除；
  配置路径内的旧文件默认 `unmanaged`。原始/声明输入
  数据、pin 结果、最近成功 run、活动/unknown 任务、有效计划、元数据、环境前缀和跨项目
  重叠根始终保护。
- `replica` 清理要求完整、逐文件哈希且当前可重新核对的保留副本；截断清单、离线目标、
  无哈希或内容变化的副本都不能作为删除依据。同机/同盘副本不作为异地备份。
- 清理与提交/推送/回传共享路径守卫；计划原子认领，防止两个进程同时执行。删除只接受
  根内普通文件，拒绝目录、glob、绝对路径、`..`、符号链接/junction 和根替换；逐项核验
  指纹后删除，并记录 journal 与 postcheck。失去回执时保持 `unknown` 与守卫，`--resume`
  只继续原清单，不重新选择候选。控制端用 OS 进程锁与 owner token/CAS 固定执行者，
  目标端用持久 generation + OS 锁拒绝迟到旧请求；心跳仅诊断，不构成超时抢锁依据。
  KeyboardInterrupt、unknown 写库失败不会释放不确定 guard。外来控制端/未知锁作用域拒绝自动接管。
- `storage show PLAN` 只读查看恢复证据；引用变化后 `storage reconcile PLAN --yes` 先确认目标
  helper 已 quiesce/fence，再只读记录路径并将旧计划设为 `superseded`，**不执行删除**。
  只有终结持久化后释放旧 guard，避免既不能恢复也不能重新规划的永久自锁。
  `.rcf-storage/operations` 是 fencing 历史，不能删除它来“解锁”；metadata 损坏/锁忙时 fail closed。

本机所有 local 节点别名与 `controller` 共用物理 guard 身份；相同 `ssh_alias`、SSH program 与连接
选项的远端别名也共用连接身份。不同 SSH alias 实际解析到同一台机器无法离线证明，必须避免重复
登记；这里不读取私有 SSH 配置或联网猜测。配置漂移/移除后旧 guard 保守阻塞重叠范围。
这些规则避免两个 RCF 清理者各自删除对方
依赖的副本。旧的未知身份 guard 保守阻塞重叠操作，不能自动丢弃。pin/最近成功保留按真实 owner
和所有相关结果目录引用检查；当前配置声明的 checkout 输入即使尚无历史 run 也受保护，误登记为
`disposable` 不会取消输入保护。

代码 push 在本地/远端都逐组件拒绝已有目录链接，避免沿目录链接写出目标根或原地修改硬链接共享
inode。POSIX 持目录 fd 并临时文件原子替换；Windows 目录句柄拒绝 WRITE/DELETE sharing，阻止原地
设置 junction。由于该严格共享策略与 Windows 原子 rename 冲突，Windows 使用已固定父目录下的
排他新文件创建：有旧文件时先保存完整恢复副本，再 unlink/写入新 inode；失败尝试恢复，无法恢复
时保留 `.previous` 备份并报错。**Windows 的单文件替换也不是原子的**，崩溃可能留下部分写入与备份，
不能据此把目标 checkout 当事务性存储。不要在活动作业读取的目录并发 push。
远端需要标准库 `python3`；不支持安全目录操作时失败关闭。仍不保证共享 FS 故障或整批原子交付。

首次登记 run 产物时会对新文件做一次有界哈希；输出极大或超时时这些文件保持未登记
（安全但不可清理），不会退化为“没有基线也能删”。

**限制**：RCF 无法判断科研价值，也不能阻止共享文件系统上非 RCF 进程的并发写入、
文件系统快照/open handle 导致的容量延迟，或同一磁盘副本无法防磁盘故障。扫描与删除
受超时/条目预算约束；遇到不完整扫描或平台无法证明目录边界时宁可拒绝。

### 1.13 提交前漂移复查

新提交复查原计划记录的 commit/dirty、环境指纹与输入清单 digest。变化或无法复查即拒绝，
要求重新 plan；幂等重放已有 run 不重复探测。不运行安装/同步/verify 用户命令。
这是轻量证据核对，不是隔离：dirty 一直为 true 时的进一步改动、同路径同大小输入内容变化，
以及复查后修改仍可能无法发现。

### 1.14 Windows Task Scheduler 长作业

- 只对显式配置了 `[nodes.<id>.windows_task]` 的 SSH/WSL 节点启用；节点仍用原 SSH
  传输/探测，长作业由 Task Scheduler 启动，作业身份仍是提交作业的用户。
- 计划任务无触发器，只在 `submit` 时手动启动；终态或取消后删除。任务名带 `RCF-` 前缀，
  不写密码、不要求管理员、不安装常驻服务。
- `cmd.exe` 包裹 `wsl.exe` 是安全/可用性要求：直接启动在任务上下文会失败；发行版名
  只允许字母、数字、点、下划线、连字符，避免命令注入和 `WSL_E_DISTRO_NOT_FOUND`。
- 取消先尝试标记吻合的 WSL 进程组信号，再停止任务；停止、查询、探测异常均保持 unknown。
  只有同一 PID 明确消失、信号已确认且 Scheduler 明确 absent/nonrunning，或已有 exit_code，
  才返回取消/终态。无 PID 不等于无作业，禁止提前删除诊断任务。
- 启动器立即保存 WSL 错误码，写 rc 后 `exit /b`；失败日志在远端从原 bytes 做有界 UTF-8/UTF-16
  解码后才走 SSH，避免中文在控制端 UTF-8 解码前丢失。计划任务仍默认只在用户登录会话运行。

**限制**：控制器崩溃可能留下已结束但未删除的 `RCF-*` 任务（无触发器，不会重跑）；
需要在机器上手工清理。未登录、关机、RDP 会话切换等 Windows 会话状态可能使任务不启动；
这类情况会以 `unknown`/启动器失败呈现，不会假报成功。

### 1.15 进度证据、试跑与预算

- 试跑的 deadline 覆盖 launcher 退出后仍存活的后代；Windows 在启动前以 Job Object 约束执行树，
  POSIX 持续观察受管进程组，不能仅凭父进程退出宣布试跑结束。
- 校准证据版本为 2，绑定解析后的 total 和契约语义。旧算法证据需要重新试跑；预算消费核对版本、
  总量与契约指纹。速率按真实推进 checkpoint 之间的完整等待计算，heartbeat 不会重置推进计时。
- Windows 控制端短命令用独立 Job Object 和文件式 stdio，超时按句柄回收受管树，不等待后代管道 EOF。
  正常完成的 dispatch helper 不清理合法 detached 后代，长期作业不使用短命令 timeout 的清理策略。

- **不伪造 ETA**：进度只来自项目声明的 JSONL 证据；没有可信推进时返回 `eta_seconds=null`。
  `time_minutes`（walltime）与 `runtime_budget_seconds` 永远不会被当作预计耗时。
- **只读采集**：进度读取是有界尾部读取，不跟随符号链接，远端只跑短命只读 helper；
  `run progress` 会先做一次只读状态刷新，不会提交/取消；
  截断/损坏会显式标记，不完整证据不能用于正式校准。
- **预算不杀作业**：正式任务超出预算只产生 `budget_exceeded` 告警，并阻止**新**计划；
  不会自动 `scancel`/杀进程。取消仍然是独立、需确认的操作。
- **试跑必须显式**：只有 `pilot_seconds`（1..300）的计划才会被硬 watchdog 包裹，
  到期以 124 终止，progress 显式给出 `pilot_timeout`；不会在提交正式任务时偷偷试跑，也不会用试跑结果冒充正式结果。
- **校准有门禁**：正式预算需要同一负载（参数/代码 commit/环境/节点/输入）的终态实测证据；
  脏工作区、无 commit、早退/崩溃、截断或收敛型任务都不会产生可用校准。校准有有效期，
  重读旧运行不会续期。
- **有界等待**：`run wait` 最长 60 秒（受 `[progress].max_wait_seconds` 约束），共享同一个
  绝对 deadline；它是长轮询，不是后台通知，超时不是失败。
- **预算时钟有证据**：优先使用 executor 写的节点 `started_at`，不把 Slurm 排队时间算成运行时间；
  终态用控制端 `finished_at` 冻结，运行中不会无限增长。文件缺失时才回退（Slurm 仍标注不可用）。
- **时钟差显式**：节点与控制端时钟不一致时，用首个样本的 `timestamp - elapsed_seconds` 估计
  worker 起点，`clock_offset_seconds` 只归一化年龄和起点，并产生 `clock_skew` 告警；
  避免把节点时钟差直接当作长时间未上报；reporter 重启/尾部截断后复用已观察偏移，
  避免长作业后半段误报 stale。

**限制**：进度是项目自己写的，恶意或错误的 reporter 可以谎报 `completed`；RCF 只能保证
「不在此基础上编造更精确的数字」，不能验证科研进度本身。时钟归一化假设 reporter 在作业启动
附近创建；reporter 很晚才启动或长时间排队后仍无 `started_at` 时，预算起点仍可能不可用。

## 2. 明确不保证的部分

| 不保证 | 说明 |
| --- | --- |
| 沙箱隔离 | 作业以你的身份运行，能读写你有权限的一切 |
| 命令白名单 | 只拦已知危险模式；`python -c "..."` 可以绕过 |
| 加密存储 | SQLite 明文；日志明文；共享盘上的 `run.log` 同组用户可读 |
| 远程防篡改 | 远程 `run.sh` 落地后若被他人改写，RCF 无法检测（可加哈希校验，未实现） |
| 多用户隔离 | 单用户控制面；多人共用一台控制机需要各自独立的 `RCF_HOME` |
| 网络层安全 | 依赖 SSH/Tailscale/EasyTier；RCF 不建隧道、不做证书管理 |
| 凭证轮换 | RCF 不管理密钥生命周期 |
| Slurm 配额/公平性 | 由调度器与管理员决定；RCF 只读展示 |
| 进度真实性 | reporter 由项目脚本写入；谎报/漏报无法被 RCF 识别 |
| ETA 准确度 | 只是基于实测速率的区间；负载变化、I/O 抖动、抢占都会让它失效 |
| 预算强制 | 预算只告警/阻止新计划；不自动终止已提交的正式作业 |

## 3. 运维检查清单

上线前建议逐条确认：

- [ ] `rcf doctor` 全绿，确认 execute/confirmation 配置；`[environments].managed_roots` 只列出你拥有的私有目录（旧 `allow_environment_mutation` 已弃用）。
- [ ] `~/.ssh/config` 里每台机器都有别名，且手动 `ssh` 过一次、核对过指纹。
- [ ] HPC 账户能 `sbatch` 一个 1 分钟的测试作业（用 `rcf run plan` 看一眼 `sbatch` 脚本）。
- [ ] `~/.rcf` 权限为 700（里面虽然没有密钥，但有节点拓扑与运行历史）。
- [ ] 共享 `work_root` 上不要放敏感数据：`run.log` 可能被同组用户读到。
- [ ] 定期 `rcf db audit` 复核 Agent 实际提交了什么。
- [ ] 首次清理前先用 `storage scan/report` 只读核对根目录；历史文件逐个 `storage adopt`，
      不要把整个 home/项目目录登记为可清理。
- [ ] 逐份复核 `storage plan` 的文件清单、保护检查与保留副本；`storage apply` 必须带 `--yes`，
      中断后先看 journal，再只用 `--resume` 继续原计划。
