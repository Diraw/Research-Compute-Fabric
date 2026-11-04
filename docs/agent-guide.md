# Agent 接入指南

目标：Agent 不需要知道任何 IP、端口、绝对路径或 Slurm 语法，就能把一次计算跑起来。

阅读顺序：准备（1）→ 接入 MCP（2）→ 工具清单（3）→ 工作循环（4）→ 项目配置（5）→
拒绝边界（6）。第 4 节最长，第一次读可以先看 4.1–4.3，再按需跳到环境、文件、存储或进度章节。

## 1. 一次性准备（人来做）

```bash
uv sync
rcf init                       # ~/.rcf/{rcf,nodes,projects,environments}.toml
```

已用 pip/venv 安装时可跳过 `uv sync`，直接执行 `rcf init`；两种方式的 `rcf` 命令等价。

编辑 `~/.rcf/nodes.toml`（示例见 `examples/`），填入每台机器在 `~/.ssh/config` 里的别名：

```toml
[nodes.cluster]
kind = "slurm"
ssh_alias = "cluster"          # 对应 ~/.ssh/config 的 Host cluster
work_root = "/home/researcher/rcf"
labels = { site = "lab", gpu = "yes" }

[nodes.cluster.slurm]
default_partition = "gpu"
partitions = ["gpu", "cpu"]
```

先手动验证一次，确认主机密钥、免密登录、Slurm 权限都正常：

```bash
ssh cluster 'hostname; sinfo -h -o "%P"'     # 人工确认一次
rcf node inspect cluster                     # RCF 只读探测
```

> RCF 永远不会替你接受一个未知主机密钥。第一次连接前请自己 `ssh` 一次并核对指纹。

节点侧要求：POSIX + **bash**（远程脚本走 `ssh <alias> bash -s`，不经过你的登录 shell——
fish/zsh/csh 都能用）、可写的 `work_root`、普通用户权限即可。

接入一台新机器后先跑 **`rcf node selftest <node>`**（约 10 秒）：它验证 ssh、多行脚本、
`work_root` 建/写/读/删、`python3`、GPU 和调度器，不提交任何作业。

## 2. 把 RCF 挂到 Agent 上

RCF 是一个 **stdio MCP server**，命令是 `rcf mcp serve`（等价 `rcf-mcp`）。

**Claude Code**（项目级 `.mcp.json`，或 `claude mcp add`）：

```json
{
  "mcpServers": {
    "rcf": {
      "command": "C:/Users/you/.local/bin/rcf.exe",
      "args": ["mcp", "serve"],
      "env": { "RCF_HOME": "C:/Users/you/.rcf" }
    }
  }
}
```

**Codex**（`~/.codex/config.toml`，字段名以你的版本为准）：

```toml
[mcp_servers.rcf]
command = "rcf"
args = ["mcp", "serve"]
env = { RCF_HOME = "C:/Users/you/.rcf" }
```

验证：`rcf mcp tools` 查看当前版本的工具清单；在 Agent 里问「列出所有计算节点」应当返回 JSON。

## 3. 工具清单

设计文档里的点号名字（`compute.list_nodes`）在多数客户端不合法（名字只允许 `[A-Za-z0-9_-]`），
所以实际注册名用下划线。两者由 `TOOL_SPECS` 一一对应，并有测试保证不漂移。当前共 31 个工具。

| MCP 工具 | 设计名 | 副作用 | CLI 等价 |
| --- | --- | --- | --- |
| `compute_list_nodes` | `compute.list_nodes` | 无 | `rcf node list` |
| `compute_inspect_node` | `compute.inspect_node` | 无（只读探测） | `rcf node inspect` |
| `compute_node_selftest` | `compute.node_selftest` | 极小：建/删临时目录 | `rcf node selftest` |
| `compute_list_environments` | `compute.list_environments` | 无 | `rcf env list` |
| `compute_inspect_environment` | `compute.inspect_environment` | 无；不执行验证命令 | `rcf env show` |
| `compute_verify_environment` | `compute.verify_environment` | **有**：需本次 `confirm=true` 与 execute 权限 | `rcf env verify ENV --node NODE --yes` |
| `compute_plan_environment` | `compute.plan_environment` | 无（只生成计划） | `rcf env plan ENV --node NODE` |
| `compute_apply_environment` | `compute.apply_environment` | **有**：需 `confirm=true`；只写自己拥有的前缀（`managed_roots` 或 `managed=true`） | `rcf env apply ENV --node NODE --yes` |
| `compute_list_projects` | `compute.list_projects` | 无 | `rcf project list` |
| `compute_plan_run` | `compute.plan_run` | 无（只写一条计划记录） | `rcf run plan` |
| `compute_submit_run` | `compute.submit_run` | **有**：提交作业、预约 GPU | `rcf run submit --yes` |
| `compute_get_run` | `compute.get_run` | 无（轮询执行器） | `rcf run status` |
| `compute_get_run_progress` | `compute.get_run_progress` | 无（有界进度采集） | `rcf run progress` |
| `compute_wait_run` | `compute.wait_run` | 无（有界长轮询） | `rcf run wait` |
| `compute_get_logs` | `compute.get_logs` | 无 | `rcf run logs` |
| `compute_cancel_run` | `compute.cancel_run` | **有**：终止作业 | `rcf run cancel --yes` |
| `compute_list_runs` | `compute.list_runs` | 无 | `rcf run list` |
| `compute_sync_project` | `compute.sync_project` | `apply=true` 时**有** | `rcf project sync` |
| `compute_push_project` | `compute.push_project` | `apply=true` 时**有**：复制代码或数据 | `rcf project push` |
| `compute_collect_results` | `compute.collect_results` | 无 | `rcf run results` |
| `compute_fetch_results` | `compute.fetch_results` | **有**：写本地磁盘 | `rcf run fetch --dest DIR --yes` |
| `compute_scan_storage` | `compute.scan_storage` | 无（只读扫描登记根） | `rcf storage scan` |
| `compute_report_storage` | `compute.report_storage` | 无 | `rcf storage report` |
| `compute_check_storage` | `compute.check_storage` | 无 | `rcf storage check` |
| `compute_plan_storage_cleanup` | `compute.plan_storage_cleanup` | 无（只生成具体文件计划） | `rcf storage plan` |
| `compute_plan_storage_adoption` | `compute.plan_storage_adoption` | 无（只生成显式路径登记计划） | `rcf storage adopt` |
| `compute_get_storage_plan` | `compute.get_storage_plan` | 无（执行阶段、journal、guards） | `rcf storage show PLAN` |
| `compute_reconcile_storage_plan` | `compute.reconcile_storage_plan` | **有**：fence 旧请求并终结计划，**不删除文件** | `rcf storage reconcile PLAN --yes` |
| `compute_apply_storage_plan` | `compute.apply_storage_plan` | **有**：登记或永久删除已审阅计划 | `rcf storage apply PLAN --yes` |
| `compute_pin_run` | `compute.pin_run` | **有**：改变保留标记 | `rcf run pin` / `rcf run unpin` |
| `compute_doctor` | `compute.doctor` | 无 | `rcf doctor` |

`compute_inspect_environment` 是纯探测入口，不能传 `verify`。执行已审阅的 `verify_command`
使用 `compute_verify_environment(..., confirm=true)` 或 `rcf env verify ENV --node NODE --yes`。
兼容的 CLI `env show --verify` 也必须带 `--yes`；即使关闭全局普通确认策略，验证命令仍要求本次确认。
普通 run 参数不得使用 `python`、`output_dir`、`run_id`、`params_file`、`script`、`launcher`、
`project_id` 或 `node_id` 等保留名。修改它们应通过项目配置/受支持的请求字段完成。

所有工具返回统一信封：

```json
{ "ok": true,  "data": { ... } }
{ "ok": false, "error": { "code": "permission_denied", "message": "...", "hint": "..." } }
```

错误码：`config_invalid`、`spec_invalid`、`not_found`、`host_unreachable`、`auth_failed`、
`host_key_rejected`、`executable_missing`、`command_timeout`、`permission_denied`、
`approval_required`、`conflict`、`scheduler_error`、`executor_error`、`store_error`。

## 4. 推荐给 Agent 的工作循环

### 4.1 基本循环

```
1. compute_list_projects            → 确认项目 ID 与入口
2. compute_inspect_node             → 只看需要的节点；关注 gpu_schedulability
3. compute_plan_run                 → 拿到 plan_id 与 checks
4. 人看命令预览（command.preview）   → 确认工作目录、参数、GPU
5. compute_submit_run(confirm=true) → 拿 run_id（同 plan 重放返回已有 run，不重投）
6. compute_get_run_progress         → phase/completed/ETA/告警；eta_seconds=null 即不可估
   compute_wait_run(until=terminal)  → 有界等待（≤60 秒），超时返回 timeout，不是失败
7. compute_get_logs                 → 失败先看日志
8. compute_collect_results          → 结果清单（原位，不搬大数据）
9. compute_fetch_results            → 取回产物（逐文件 SHA-256；保存 copy receipt）
10. compute_scan_storage             → 更新文件台账；report/check 看增长与预算
11. compute_plan_storage_cleanup     → 只生成具体文件、原因和保护检查，不删除
12. 人复核文件清单                    → 确认后才 compute_apply_storage_plan(confirm=true)
```

**不要**跳过 3、4 直接提交；`submit` 只接受 `plan_id`，不接受临时拼的命令行。
这是刻意的：所有执行都必须先有一次可审阅的计划。

三条与提交有关的硬规则：

- **plan 会过期**：默认 30 分钟（`[runs].plan_ttl_minutes`）。过期提交返回 `plan_expired`，
  正确反应是**重新 plan**，不要试图复用旧 plan——它描述的是 30 分钟前的机器。
- **输入必须声明**：项目声明了 `inputs` 时，`compute_plan_run` 会逐个 pattern 在节点上展开。
  任一个匹配不到文件就是 `blocked`（`project_inputs`），此时不要提交，先确认数据是否同步。
  计划里 `inputs` 字段给出文件数、总字节和 digest，等同「这次运行读的是哪批数据」。
- **状态有新鲜度**：`compute_get_run`/`compute_list_runs`（以及 `rcf run status/list --json`）
  返回 `observed_age_seconds`、`stale` 和 `state_observed_at`。`state` 只代表最后一次**成功**探测
  看到的状态；探测失败时 RCF 保留旧状态并记录 `error`。`stale: true` 表示该状态已超过
  `[runs].stale_after_seconds`（默认 300 秒）没被确认，此时不要把它当实时状态，更不要据此
  判断「还在跑」或「已经结束」。

### 4.2 存储治理的五条硬规则

- **扫描不是删除授权**：`scan`/`report`/`check`/`plan` 全部无副作用；计划过期、引用变化、
  pin、活动任务、文件指纹变化都会使 apply 拒绝。
- **未知归属默认保护**：历史目录内容不会因为路径在配置里就被认领；需要 `storage adopt`
  明确列出文件并确认登记，登记本身也不删除。
- **原始数据与唯一结果不按年龄清理**：数据根、声明输入、最近成功 run、pin 的结果和缺少
  完整保留副本的产物默认受保护。`disposable_globs` 只能声明中间文件。
- **代码只清 RCF 部署过的残留**：本地删掉/改名、远端未被手工修改、且没有活动引用时才进入
  `stale-code` 候选；远端手工文件和修改过的脚本保留。
- **中断后只恢复原计划**：先 `storage show PLAN` 查看 owner/stage/journal/guards。
  `apply PLAN --resume --yes` 必须取得原控制端 OS 锁并 fence/quiesce 目标 helper，才复核原清单；
  不凭心跳时间或 PID 是否存在抢占其他控制端。引用变化时不直接继续删，改用
  `storage reconcile PLAN --yes` 只观察/终结旧计划、释放旧守卫，再重新 plan。

### 4.3 触发提示词示例

> 「用 RCF 在本机跑公开 CPU 示例 `demo`，seed=1、steps=1000，walltime 上限 1 分钟。
> 先给我执行计划，我确认后再提交。」

Agent 先列出项目/节点，再用 `compute_plan_run` 的 `params={"seed":1,"steps":1000}`、
`node_id="local"` 与 `requirements={"gpus":0,"time_minutes":1}` 生成计划，
把 `command.preview` 和 `checks` 展示给你。

注意 `time_minutes` 是调度器时限/上限，**不是预计耗时**；超过 5 分钟的任务如果没有进度契约，
计划会直接 `fail`。需要时用第 4.9 节的方式先声明契约与预算。

### 4.4 环境怎么锁

环境是运行条件的一部分，不是背景噪音：

```
rcf env show <env> --node <node>          # 看状态与指纹（compatible / missing / drifted）
rcf env lock <env> --node <node>          # conda list --explicit 或 pip freeze，可据此重建
rcf env lock <env> --node <node> --out lock.txt

rcf env plan <env> --node <node>          # 缺包时生成安装计划（无副作用）
rcf env apply <env> --node <node> --yes   # 执行审阅过的计划；只写自己拥有的前缀
```

Agent 侧对应 `compute_plan_environment` → `compute_apply_environment(confirm=true)`。
能不能写不取决于 `allow_environment_mutation`（那是旧字段，不参与判定），而取决于
`[environments].managed_roots` 是否包含该路径、或该环境是否标了 `managed = true`；
计划被拒时 `blockers` 会给出具体原因。

`compute_get_run` 返回的 `env_fingerprint` 与 `git_commit` 一起，构成「这次结果是在哪份代码、
哪套依赖下产生的」。要复现实验，先对齐这两个值。

### 4.5 文件怎么到机器上

两条路，按「代码有没有远端」选：

```bash
# 有 git 远端：拉/切 commit（拒绝在脏工作区上动手）
rcf project sync <project> --node <node>            # 只看计划
rcf project sync <project> --node <node> --apply --yes

# 没有远端，或改完还没提交：按内容差异复制
rcf project push <project> --node <node>                      # 默认本地 → 目标
rcf project push <project> --node <node> --from workstation     # 节点 → 节点
rcf project push <project> --node <node> --from workstation --yes

# 数据是另一个根，单独走
rcf project push <project> --node <node> --from workstation --data --yes
```

代码和数据永远分开：`--data` 读的是 `data_paths`，不会碰 checkout；反向地，普通 push 也不会把
数据目录带进代码目录。项目里用 `inputs = ["data:init/*.dat"]` 声明数据依赖，plan 期就会去数据根校验。

`push` 的语义是刻意保守的：

- 只发新增和内容不同的文件；判等用大小 + 哈希，不看时间戳；
- 目标上**更新**的文件不覆盖，只在计划里列出（`--force` 才覆盖）；
- 从不删除目标上的任何文件；
- 落地后逐文件比对 SHA-256，不一致直接报错；成功部署的逐文件回执进入文件台账。

旧代码的清理由独立的 `storage plan --kind stale-code` 生成具体计划，不在 push 中顺手删除。

### 4.6 文件台账与清理

```bash
rcf storage scan <project> --node <node>              # 更新代码/数据/run 产物台账
rcf storage report <project> --node <node> --largest 20
rcf storage check <project>                           # 过期、预算与不完整扫描提醒

# 旧代码：只在目标仍是 RCF 部署的同一内容、源端确实已删除时才成为候选
rcf storage plan <project> --node <node> --kind stale-code --from <source-node>

# 产物：先声明可丢弃的中间文件，再按 run 选择
rcf storage plan <project> --node <node> --kind disposable --run <run_id>
rcf storage plan <project> --node <node> --kind replica --run <run_id>

# 历史目录：必须逐个 --path；登记不等于删除
rcf storage adopt <project> --node <node> --root data --path old/result.dat --kind output
# 历史代码：必须同时给 --from，且目标文件要与该源 checkout 逐文件哈希一致
rcf storage adopt <project> --node <node> --root code --path old.py --kind code --from <source-node>
rcf storage apply <plan_id> --yes
```

清理计划会列出每个文件的路径、大小、原因、保护检查和保留副本。以下情况一律拒绝：计划过期、
引用/pin/归属变了、文件指纹变了、扫描不完整、源目录重新出现、保留副本失效，或平台无法
安全执行根目录内删除。中断后先 `storage show <plan_id>`，再用
`storage apply <plan_id> --resume --yes` 继续**同一份清单**。若原引用指纹已变化，先
`storage reconcile <plan_id> --yes`：只停止旧代次的删除资格、只读记录剩余路径，旧计划变为
`superseded` 后再重新 plan。不能删除 `.rcf-storage` 或手改数据库来“解锁”。
正常计划过期拒绝；中断计划显式 resume 时可超原有效期，但必须重新核对全部原对象与引用。

`replica` 只接受完整、逐文件校验且当前仍可重新核对的保留副本；截断清单、离线目标、
没有哈希或已变化的副本都不能作为删除依据。同机副本不等于异地备份。

### 4.7 多进程（MPI）

entrypoint 里用 `{launcher}`，plan 时给 `--tasks`：

```bash
rcf run plan --project hpc-mpi --node cluster --cpus 4 --tasks 4
```

`{launcher}` 展开成 `srun` / `mpiexec` / `mpirun`，由环境与执行器选择；
具体 MPI/PMI 组合必须在调度器分配内核对 communicator size（见 `docs/hpc-lammps.md`）。
`tasks=1` 时它展开为空，命令里不会留下空参数。

**别自己写 `srun -n 4`**：如果环境的 MPI 没有 PMI 支持，4 个进程会各自以为自己是独立作业，
不报错、结果错。用 `{launcher}` 或显式设 `mpi_launcher`。

### 4.8 Windows + WSL 长作业

Windows OpenSSH 在会话结束时杀掉进程树，普通 SSH 执行器的 `setsid/nohup` 在那里不成立。
给节点加一个 `windows_task` 块，文件操作仍走 SSH/WSL，长作业改由 Windows Task Scheduler 启动：

```toml
[nodes.windows-wsl]
kind = "ssh"
ssh_alias = "windows-wsl"
work_root = "/mnt/e/rcf"
scp_path_map = { "/mnt/e" = "E:/" }

[nodes.windows-wsl.windows_task]
distro = "Ubuntu"          # wsl.exe -l -v 里的名字；不要加引号
# windows_work_root = "E:/rcf"   # 默认从 scp_path_map 推导
```

提交和普通节点一样：

```bash
rcf run plan --project demo --node windows-wsl --param seed=1
rcf run submit <plan_id> --yes
rcf run status <run_id>
rcf run logs <run_id>
rcf run cancel <run_id> --yes
```

启动链是 `Task Scheduler -> cmd.exe -> wsl.exe -> bash rcf-task.sh`。计划任务没有触发器，
提交时手动启动，终态或取消后删除。它默认只在用户登录会话运行，不需要管理员权限；
如果 Windows 机器未登录或 WSL 发行版名不对，状态会是 `unknown`/启动失败，不会假报成功。
定期检查残留任务：

```powershell
Get-ScheduledTask -TaskName 'RCF-*' | Select-Object TaskName,State
```

### 4.9 进度、ETA 与耗时预算

长任务不能只靠「等它结束」。RCF 把进度当成**证据**，而不是把 walltime 当作 ETA：

```bash
rcf run progress <run_id>          # 先只读刷新状态，再给 phase/completed/ETA/告警
rcf run wait <run_id> --timeout 30 --until terminal
rcf run wait <run_id> --timeout 30 --until alert   # 只为新的停滞/超预算告警醒来
# --timeout 上限 60 秒；若 [progress].max_wait_seconds 更小，以配置为准
```

`eta_seconds=null` 是合法且常见的答案：刚启动、刚重启、只有心跳没有推进、收敛型任务、
进度文件损坏、或远端证据读不到时，RCF 不会编一个数字。`alerts` 里的
`progress_missing`、`clock_skew`、`pilot_timeout`、`stalled_progress`、`budget_at_risk`、`budget_exceeded` 都只是告警，
**不会终止正式作业**。节点与控制端时钟差会被归一化并显式报告，不会误报成停滞。

#### 项目侧要做什么

下面是长任务的改造模板，不是公开短 CPU 示例已实现的功能。
须先给自己的入口接入 reporter，再扩展 `demo` 配置；不能只复制 TOML 就声称产生进度证据。
在 `projects.toml` 里声明完成条件与进度口径：

```toml
[projects.demo.progress]
mode = "steps"                 # steps / phases / convergence
total_param = "steps"          # 从本次 params 读总量；或写死 total = 1000
completion_condition = "全部 step 完成、result.json 写出且进程退出 0"
heartbeat_timeout_seconds = 120
stall_timeout_seconds = 300
```

脚本里用随 RCF 附带的 `reporter`（零依赖，可单独复制到项目，不要求目标是安装 RCF）：

```python
from reporter import Reporter   # 把 src/rcf/progress/reporter.py 复制到项目里

with Reporter(total=total_steps) as progress:   # RCF 会注入 RCF_PROGRESS_FILE/RUN_ID/TOTAL
    for step in range(1, total_steps + 1):
        train_step()
        progress.update(step)          # 只有推进写 completed；心跳用 progress.heartbeat()
```

`finished=True` 只表示某个 phase 完成，不是整个 run 成功；run 的终态仍由执行器与退出码决定。
MPI 作业只在 rank 0 写进度，其他 rank 不要打开同一个文件。

#### 正式预算怎么过

只声明 `runtime_budget_seconds` 不够，RCF 要求**同一负载**的实测证据：参数、代码 commit、
环境指纹、节点/分区、输入清单都进入校准 key；改其中任何一项都要重新试跑。

```bash
# 1. 显式限时试跑（1..300 秒，硬 watchdog，退出码 124；不是正式作业）
rcf run plan --project demo --param seed=42 --param steps=200000 --pilot-seconds 60
rcf run submit <pilot_plan_id> --yes
rcf run progress <pilot_run_id>

# 2. 用试跑留下的校准提交正式计划（参数/总量必须与试跑完全一致）
rcf run plan --project demo --param seed=42 --param steps=200000 \
  --runtime-budget-seconds 21600
rcf run submit <plan_id> --yes
```

规则：

- 试跑必须显式给 `--pilot-seconds`，且是独立计划、独立确认；不会因为你提交正式任务就偷偷试跑。
- 校准只在真实推进、单一 session/phase、`total` 匹配、速率稳定且试跑确实消耗了大部分时限时生成；
  早退、崩溃、读取截断、收敛型任务都不会产生可用校准。
- 校准有时间上限（默认 168 小时）；重读旧运行记录**不会**给它续期。
- 没有匹配校准、代码工作区脏或没有 commit 时，正式计划直接 `fail`，不是警告。
- 真正无法改脚本的短任务（≤5 分钟）可豁免契约；这不会产生 ETA，也不会当作预算证据。

#### Agent 的诚实准则

- 报 ETA 时必须带 `eta_range_seconds` 与 `confidence`；不要把 `time_minutes`/walltime 说成预计耗时。
- 不要为了让计划通过而填一个「看起来合理」的试跑时长或预算；先解释缺什么证据。
- `wait` 超时只是「这段时间内没有变化」，不是任务卡死，更不是失败。

## 5. 项目该怎么写（让入口可被自动发现）

`projects.toml` 里的 `entrypoint` 是一个**模板**，RCF 会先按空格分词、再逐词替换占位符
（所以参数里的空格不会把命令拆坏）：

```toml
[projects.demo]
# 对应 examples/demo/compute.py；路径替换成实际 checkout。
entrypoint = "{python} {script} --seed {seed} --steps {steps} --out {output_dir}"
script = "compute.py"
env_id = "system-py"
outputs = "outputs"
paths = { "local" = "/path/to/checkout/examples/demo", "cluster" = "/home/researcher/projects/demo" }
# git_url 仅在你已有经审阅的真实远端时私下配置；不提供虚构仓库。
```

可用的占位符：

| 占位符 | 含义 |
| --- | --- |
| `{python}` | 解释器（配合环境时由 PATH 解析） |
| `{script}` | `project.script` |
| `{output_dir}` | 本次运行的输出目录（已创建）。**默认契约要求 entrypoint 必须用到它** |
| `{params_file}` | `params.json` 绝对路径（RCF 已写入本次参数） |
| `{run_id}` / `{project_id}` / `{node_id}` | 标识 |
| 任意参数名 | `params` 里传进来的标量 |

程序里可用的环境变量：`RCF_RUN_ID`、`RCF_PROJECT_ID`、`RCF_NODE_ID`、`RCF_OUTPUT_DIR`、
`RCF_ENV_ID`、`CUDA_VISIBLE_DEVICES`（非 Slurm 节点由 RCF 指定）、`RCF_GPU_KEYS`。
声明了进度契约时还有 `RCF_PROGRESS_FILE`（本次 run 的 JSONL 路径）、`RCF_PROGRESS_TOTAL`、
`RCF_PROGRESS_MODE`、`RCF_PROGRESS_PILOT`（试跑时为 `1`）。

同一个项目跑在不同机器上、环境机制不同时，用 `env_by_node` 覆盖：

```toml
[projects.hello]
env_id = "ws-conda"
env_by_node = { "cluster" = "hpc-py" }   # HPC 用 module，工作站用 conda
```

**产物契约**（`output_contract`，默认 `argument`）：

- `argument`：entrypoint 必须把 `{output_dir}` 传给脚本，否则 `rcf run plan` 直接 `[x]` 阻塞。
  理由：找不到产物的 run 无法复现、无法比较、无法迁移。
- `env`：脚本自己读 `$RCF_OUTPUT_DIR`（RCF 总是导出它）。
- `none`：老脚本改不动，显式放弃契约；`rcf run results` 会改为列出「项目目录里本次改动过的文件」。

计划阶段的检查只能证明「模板里提到了输出目录」。**运行结束时会再验一次**：成功的 run 如果输出目录
仍然是空的，会被标记 `contract_violation` 并写一条 `output_contract_violation` 事件
（`rcf run status` 会显示黄色提示）。`none` 契约不参与这项检查——空输出是预期行为。

**建议**：脚本把 `RCF_OUTPUT_DIR` 当作唯一输出根目录，把关键参数写进输出目录里的
`run.json`（RCF 也会写一份），这样单看输出目录就能复现。

## 6. 边界与红线

Agent 会遇到的拒绝（都是设计如此，不是 bug）：

| 情况 | 返回 |
| --- | --- |
| `submit` 没带 `confirm=true` | `permission_denied`（confirmation_missing） |
| 计划里有 `fail` 检查项 | `permission_denied` + 全部阻塞项 |
| 目标 GPU 已被另一个 RCF 运行预约 | `conflict` + 持有者 run_id |
| 提交前代码/环境/输入证据漂移或无法复查 | `conflict` + evidence + 重新 plan 提示 |
| sbatch 成功但没有可信 job ID | `submission_uncertain`；run 保留 unknown，可用 `run status` 尝试恢复 |
| 提交连接中断 | 原连接错误；先查询该 run，不要新建计划盲目重跑 |
| unknown 无恢复证据时取消 | `conflict` 或保留非终态；租约不释放 |
| 命令里出现 `sudo` / `rm -rf /` / `curl | bash` | `permission_denied`（destructive pattern） |
| 修改系统 Python 或 module 树 | `permission_denied`（tier 未开启） |
| 想直连计算节点绕过 Slurm | 没有这个工具（只有 `sbatch/scancel`） |

RCF **不会**：

- 未经授权安装远程依赖；系统配置、创建隧道与提权不在当前能力范围；
- 在未经 `confirm` 的情况下提交或取消；
- 把 SSH 私钥/密码写进任何文件。
