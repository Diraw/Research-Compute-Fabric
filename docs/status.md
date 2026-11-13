# 功能完成度与验收状态

## 2026-11-13：search-only 祖先目录推送修复（已合入 main）

- 分支 `fix/search-only-ancestors-20261113`；仅修 Linux 安全提取器目录句柄：
  支持 `O_PATH` 时不额外要求祖先目录 list 权限，保留 `O_DIRECTORY|O_NOFOLLOW`、
  `fstat` 目录验证、fd 锚定、归档成员和目标替换校验；Windows 分支不改。
- Windows/Python 3.11.14：
  `.venv/Scripts/python.exe -m pytest tests/unit/test_push_search_only_ancestors.py tests/unit/test_executor_push_security.py tests/unit/test_project_push.py tests/unit/test_push_integrity.py`
  → **72 passed / 2 skipped**（真实非 root Linux 权限测试在 Windows 明确 skip）。
- 经 RCF 在 Linux 非 root 用户 uid1004 下，旧/新内嵌提取器临时目录对照：
  **旧版复现 EACCES，修正版提取及原子替换成功**；root/ancestor/nested/file 链接、
  归档 symlink/hardlink 仍拒绝，实际无写权限仍拒绝。run `run_20261113T142956Z_c91c33`
  succeeded。不是对活动 MCP 已生效或真实 HPC 推送的声明。
- `.venv/Scripts/python.exe -m ruff check src tests tools`：通过；`git diff --check`：通过。
- 扩展 `.venv/Scripts/python.exe -m pytest -m "not integration" -n 2`：
  **1141 passed / 14 skipped / 1 failed**；失败为未修改的 Windows pilot descendant-heartbeat
  用例（generation2/8s）。串行 pilot 模块复验在 generation1/8s 也失败；隔离未修改 HEAD
  ab93527 的 generation1/8s 单测通过，随后修复树该单测也通过。结果与时间敏感性相容，
  但未证明失败原因；**不宣称全量回归通过，不修改/弱化该测试**。
- 活动 MCP 重载后真实 HPC managed-push 验收已通过：独立项目推送 42 字节 `proof.txt`，
  目标 SHA-256 `af346ad286a5790bf397ffedb9aa3cc67c8a6f0974c600d420805e6cb121a264`，
  报告 `verified=true`；随后独立 benchmark 132 个代码文件及 4 个数据文件推送亦验证通过。
  未修改共享祖先权限或干预其他任务。

---

> 更新：2026-11-06。**当前工作树离线集成验收通过**，不是已上传/已发布版本。
> 2026-11-07 晚追加：分区容量与 `MaxTime` 校验、`run reconcile`、失败诊断兜底、
> 取消后的退出码修正。全套离线回归重跑：**1050 passed / 12 skipped** + **22 passed**。
> 另在 lab-hpc 完成两项真机验收：GPU 驱动级显存往返（job 267665）与
> PENDING → RUNNING 转换（job 267668）。
> 测试环境：Windows / Python 3.11.14（2026-11-06）；macOS arm64 / Python 3.12.16
> 是 2026-11-04 的真实节点记录，本轮改动未在 macOS 复跑。
> 原生 Linux 与远端 GitHub CI 未在本轮执行。
> 下表命令按本机 Windows 记录；其他平台把 `.venv/Scripts/python.exe` 换成
> `.venv/bin/python`，或使用等价的 `uv run python`。

**一句话结论**：当前工作树的离线回归通过；真实设备已有私域运行样本，但公开版本仍不声明
特定站点兼容性；跨平台 CI 与正式发布未声明。

## 1. 状态定义

- **已有实现/回归入口**：源码包含该能力及测试；不等于当前工作树全套验收通过。
- **离线模拟**：本机子进程、临时目录、假 SSH/Slurm，不连接真实设备。
- **待验收**：最终集成、构建或跨平台执行尚未在此记录。
- **真实设备验证**：需要独立授权与脱敏证据；公开版本目前不声明特定设备兼容性。

## 2. 能力与可定位回归

| 能力 | 实现/测试入口 | 当前声明边界 |
| --- | --- | --- |
| 配置、交叉引用与安全策略 | `core/config.py`、`core/models.py`、`test_config_and_models.py`、`test_security.py` | 本轮离线回归通过；`doctor` 新增本地项目路径存在性检查，会话目录被清理后能立即暴露 |
| 资源探测与 Slurm 解析 | `resources/`、`test_parsers.py`、`test_status_probe.py`、`test_real_slurm_outputs.py` | 已加入真实 CentOS 7 / Slurm 20.11 的 `sinfo`/`sacct` 夹具（脱敏）；`%F` 可用节点改按 idle 计算；`squeue` 空样本待有作业时采集 |
| 本机任务生命周期 | `executors/local_exec.py`、`test_local_workflow.py` | 真实本机子进程，不等于远程验收 |
| SSH / Slurm 执行器 | `executors/ssh_exec.py`、`executors/slurm_exec.py`、`test_ssh_slurm_workflow.py` | 假主机/调度器 + 真 Bash；真实权限/配额待授权测试 |
| Windows Task Scheduler | `executors/windows_task.py`、`test_windows_task_executor.py`、`test_windows_task_safety.py` | 登录会话、WSL、取消证据等设备行为待验收 |
| CLI / MCP | `cli/main.py`、`mcp/server.py`、`test_cli_and_mcp.py` | 工具数量与入口以当前 `rcf mcp tools` 为准 |
| 提交、租约与不确定状态 | `runs/`、`test_submission_safety.py`、`test_submission_preflight.py` | unknown 不等于失败；先恢复核查再考虑新计划 |
| 观测语义（新鲜度、错误生命周期、ssh 诊断） | `runs/manager.py`、`runs/store.py`、`core/runner.py`、`test_observation_truthfulness.py` | 2026-11-04 新增：每次探测确认写 `state_observed_at`，`run list/status` 返回 `observed_age_seconds` 与 `stale`；成功探测清掉旧 `error` 并记 `observe_recovered`；`get_run`/`run status` 顶层暴露最近一条 `diagnosis`，`run list` 也批量返回（单次 SQL，无 N+1）；ssh 255 的 detail 保留 stderr；旧库中 `succeeded` 行的陈旧观测错误一次性清除。2026-11-07 追加：`rcf run reconcile` 按 `--older-than-minutes` 重新探测陈旧的未终态 run（`--limit` 限流，CLI-only）；无匹配模式的失败记 `exit N: no known failure pattern; last log line: ...`，不再留空 |
| 失败分类（谁是责任方） | `runs/failure.py`、`runs/manager.py`、`test_failure_classification.py` | 2026-11-08 新增：终态时按日志与退出码判 `domain` / `environment` / `input` / `parameter` / `code` / `unknown`，写入 run 的 `failure_kind` 列；`run list --kind` 可筛，`run status` 显示 `verdict`，事件 data 带 `kind`。规则只看证据：结构化 verdict JSON、缺失模块/命令/GLIBC、参数被拒、traceback、测试套件汇总；都没有就是 `unknown`，不猜。历史 run 用 `rcf run classify` 从节点上仍存在的日志补判，事件标 `retrospective: true`，状态与时间戳一律不动 |
| 存储锁可视与释放（`rcf storage guards` / `unguard`） | `storage/ledger.py`、`storage/manager.py`、`security/policy.py`、`test_storage_guards.py` | 2026-11-08 新增：guard 无过期时间，中断的 fetch/cleanup 会永久锁住根目录。`guards` 列出持有者与年龄，`unguard` 需 `--yes` 并写审计。冲突消息改为「谁持有 + 怎么放」。真机：killed fetch 留下 2 个 guard，释放后重试成功 |
| 大文件 / 断线取回 | `runs/fetch.py`、`executors/ssh.py`、`test_storage_fetch.py` | 2026-11-08 真机验收：269 MB / 204 文件，fetch 52–63 s（裸 scp 30 s，节点打包 10 s），sha256 逐文件校验通过。杀 scp → 「was interrupted」；杀进程 → guard 遗留 + 节点 269 MB 归档 + 本地 97 MB 临时目录；已自愈（下次 fetch 回收归档、清理本 run 的临时目录） |
| 报告的时间维度 | `service.failure_trend`、`report._trend_section`、`test_report.py` | 2026-11-08 新增「When each kind appeared」：失败类别 × 日/周矩阵。跨度 >14 天自动转周；每列带该桶完成 run 数作分母；超过 12 桶把更早的历史折叠成 `earlier` 一列而不是丢弃。不画折线——40 条失败不足以支撑斜率 |
| 运行交付件（`rcf run manifest`） | `runs/manifest.py`、`service.run_manifest`、`test_manifest_and_diff.py` | 2026-11-08 新增：单 JSON，含 run/command/params/code/environment/inputs/outputs；输出清单实时读取，读不到写 `listed: false` + `error`，不写成空列表；默认算 sha256。2026-11-08 真机：lab-hpc run 产出 `result.txt` 32 字节 + sha256 |
| 运行对比（`rcf run diff`） | `runs/diff.py`、`service.diff_runs`、`test_manifest_and_diff.py` | 2026-11-08 新增：参数逐键（changed / only left / only right / same）、provenance、outcome、command 按 argv 比较；`--artifacts` 才比产物且两侧都算 hash——同尺寸不同内容必须报 changed。真机：两条 `hello` run 的 `result.txt` 同为 32 字节但 sha256 不同，正确判为 changed |
| 参数扫描（`rcf run sweep`） | `runs/sweep.py`、`service.plan_sweep`、`test_sweep.py` | 2026-11-08 新增：`--grid name=v1,v2` 笛卡尔展开，`--param` 固定值；整张网格先全部 plan，任一点有 fail 检查则整体拒绝，不下单。run 记 `sweep_id`，`run list --sweep` 取回同一批；`--yes` 才提交。2026-11-08 真机：sweep `sweep_20261108T043757Z_ab5647` 的 2 个组合（seed 11/12）均 succeeded、exit 0 |
| 只读报告（`rcf report`） | `report.py`、`service.build_report`、`test_report.py` | 2026-11-08 新增：单文件 HTML，无服务、无 JS、无外链；内容全部来自本地库，生成时刻写在页头。未重新观测的状态显示为 `unverified <age>`，未分类失败显示 `unknown`，不空白、不绿灯。`--project` / `--since` 先过滤再取 `--limit` |
| 产物契约语义 | `runs/manager.py`、`test_planner_and_manager.py` | `command_override` 的 run 不再误报 `contract_violation`，改记 `output_contract_unchecked`；`output_contract=none` 记 `output_contract_unmanaged`；真正的空产物仍标记违规 |
| 复现元数据传递 | `runs/manager.py`、`test_conventions.py`、`test_planner_and_manager.py` | plan 的 git/inputs 完整写入 run；新增 inputs 摘要传递契约测试；未声明 inputs 或非 git 项目保持 warn，不伪造字段 |
| plan 期资源校验 | `resources/inspector.py`、`runs/planner.py`、`test_parsers.py`、`test_planner_and_manager.py` | 2026-11-06 修正：Slurm 的 `cpu_capacity` / `memory_capacity` 改用目标分区每节点 `%c` / `%m`，不再拿登录节点冒充计算节点（此前 64 核请求被判 fail、300 GB 请求被判 ok）；分区信息缺失时降为 warn 并在 detail 写明 `login node`。新增 `partition_time_limit` 比对 `MaxTime`，`infinite` / `PARTITION_LIMIT` 不当作上限 |
| 代码同步与内容校验 | `projects/push.py`、`test_push_integrity.py` | 不宣称消除恶意共享目录并发 TOCTOU |
| 结果回收与存储治理 | `runs/fetch.py`、`storage/`、`test_storage_*.py` | 有界扫描/归属/指纹/守卫；fetch 目标不得与其他 run 的 result/work/run 目录重叠（同物理节点，含本地别名与 macOS 大小写）；保留副本按登记归属验证；不可当通用备份或垃圾回收器 |
| 进度、ETA、试跑、预算 | `progress/`、`test_progress_*.py`、`test_pilot_*.py` | reporter 是项目证据，不保证工作量或 ETA 真实性/精确性 |
| 源码交付与构建 | `test_delivery.py`、`test_release_check.py`、`tools/release_check.py` | 离线预检已通过，最终候选以 evidence.json 为准 |
| 真实路径集成（opt-in） | `tests/integration/`、`test_readonly_smoke.py`、`test_local_submit.py`、`test_remote_submit.py` | 默认 skip；2026-11-04 只读 smoke 已在两个真实节点通过，隔离 home 的真实本机 60 秒全链路通过；2026-11-07 远端 cancel（运行中与排队中各一次）在 lab-hpc 真机通过；集成模板改为轮询终态 |
| 公开材料隐私 | `tools/publication_check.py`、`test_publication_hygiene.py` | 启发式，不扫描 Git 历史，不证明无秘密 |

## 3. 最终验收记录

修复涵盖清理副本/真实归属/声明输入、路径链接写入、取消身份、环境权限与路径、试跑后代、
工作量校准与心跳速率、参数保留名、Windows shell 与 Runner 超时；独立复核追加的同 SSH endpoint
别名与原地 reparse 写入也已补回归。2026-11-06 追加 fetch 目标守卫、副本归属证据与 POSIX
私有权限；Windows 离线回归重新通过，macOS/Linux 未复跑。当前 MCP 工具数为 31，校准证据版本为 2。

| 项目 | 命令/证据 | 结果 |
| --- | --- | --- |
| 离线全套（Windows） | `uv run --frozen --offline pytest -m "not integration" -n auto --ignore=tests/e2e/test_pilot_workflow.py --ignore=tests/unit/test_runner.py`；`uv run --frozen --offline pytest tests/e2e/test_pilot_workflow.py tests/unit/test_runner.py` | **1034 passed / 12 skipped**，85.56 秒 + **22 passed**，44.72 秒（2026-11-06，Python 3.11.14） |
| 离线全套（Windows，本轮） | 同上两条命令 | **1050 passed / 12 skipped**，79.40 秒 + **22 passed**，45.72 秒（2026-11-07，Python 3.11.14） |
| 离线全套（macOS arm64） | `.venv/bin/python -m pytest -m "not integration" -o addopts="--strict-markers" -q -p no:cacheprovider` | **994 passed / 43 skipped**，114.78 秒（2026-11-04 记录；本轮未复跑） |
| POSIX 私有权限（WSL Ubuntu 24.04） | `PYTHONPATH=src pytest -q tests/unit/test_store.py::test_state_database_is_private tests/unit/test_config_and_models.py::test_starter_home_is_private` | **2 passed**（2026-11-06，Python 3.12.3）；只覆盖两个权限断言，不是 Linux 全套 |
| 公开材料扫描 | `uv run --frozen --offline python tools/publication_check.py --source . --include-code` | **通过，0 findings**；启发式，不扫描历史 |
| 源码/测试扫描 | 覆盖 src/tests/tools，合成安全测试使用运行时构造数据 | **通过，0 findings** |
| 外部可用示例 | `uv run --frozen --offline python examples/quickstart.py` | **通过**；临时独立 home、本机 CPU 作业，确定性结果核对成功 |
| Ruff | `uv run --frozen --offline ruff check --no-respect-gitignore src tests tools` | **通过** |
| 真实路径集成（2026-11-04） | `RCF_IT=1 ... pytest tests/integration/test_readonly_smoke.py`；`RCF_IT=1 RCF_IT_JOB_SECONDS=60 ... test_local_submit.py` | **通过**；只读 smoke 覆盖两个真实节点；本机 60 秒 plan → submit → observe → results → fetch 全链路通过；远端提交/cancel 默认 skip |
| 真实 Slurm 夹具（2026-11-04） | `tests/fixtures/slurm/`、`test_real_slurm_outputs.py` | **通过**；真实 `sinfo` 的 `%F`/`+` 内存后缀/`(null)`/`infinite` 与 `sacct` 的 `.batch`/`.extern` 行均被覆盖 |
| 离线候选构建 | `uv run --frozen --offline python tools/release_check.py --source . --output .tmp/release-candidate-*` | **通过**；源码清单、wheel/sdist、隔离安装与 31 工具验证；evidence.json 留在本机，不纳入公开源码 |
| 三平台 CI | Windows/Linux/macOS × Python 3.11/3.12 workflow | 已配置不等于已执行；远端结果未声明 |
| 真实取消（2026-11-07） | lab-hpc，CLI `run plan/submit/cancel`：job 267660（运行中）与 267662（排队中）均在 2 秒内得到终态与 `scancel` 回执；修正退出码后 job 267664（运行中）复验 | **通过**；同时发现并修正「取消后记 exit 0」：现在只取 `.batch` 行的信号，该行尚未落盘时记无退出码，不再伪造 0 |
| GPU 驱动级（2026-11-07） | lab-hpc `gpu` 分区，job 267665，`tests/integration/assets/gpu_cuda_smoke.py`：`cuInit` → `cuCtxCreate` → 64 MiB `cuMemAlloc` → H2D/D2H 往返 | **通过**；RTX 3090 24259.7 MB，`CUDA_VISIBLE_DEVICES=0`，mismatches 0；不声明 CUDA 内核或数值正确性 |
| PENDING → RUNNING（2026-11-07） | 3 个 @8 GPU 作业争用 2 节点 `gpu` 分区：第 3 个 16:20:07 提交为 pending → 16:20:46 running（gpu1）→ 16:21:34 succeeded | **通过**；首次拿到真实非空 `squeue` 夹具与排队时长（39 秒） |
| 长作业 + 观测断线（2026-11-07） | lab-hpc `debug` 分区，job 267670：`sleep_heartbeat.py --seconds 3900`，16:26:44 提交 → 17:33:03 成功（66.3 分钟，exit 0，131 条心跳）；16:26:47 后 14 分钟无观测 | **通过**；`run status --no-refresh` 显示 `running` + `14m STALE`，日志仍在增长；一次 `run reconcile` 恢复为 `running` 并刷新年龄；终态 `succeeded`，`active_leases` 为空，全程无 `observe_failed` |
| 真实设备 | 私域多节点运行样本（Slurm / SSH / WSL / macOS） | 已有运行证据；公开版本不声明特定站点兼容性；存储清理、进度校准仍待真机验收 |

Windows Bash 用例通过 `tests/bash_support.py` 选择 Git Bash；缺少平台能力的 skip 必须保留
具体原因。测试禁止连接真实服务器，不为通过而放宽断言，不安装依赖、不清空用户工作树。
2026-11-06 的改动只在 Windows 复跑；macOS/Linux 行保留 11-04 证据，不代表本轮代码。
`test_pilot_workflow.py` 与 `test_runner.py` 断言亚秒/5 秒墙钟终止，CI 将它们单独串行
执行，避免并行竞争把解释器启动延迟误判成看门狗或超时逻辑失败。

## 3.1 失败分类的实测分布（2026-11-08）

对已有 40 条失败 run 用 `rcf run classify --refresh` 从真实日志补判，结果：

| 类别 | 数量 | 典型证据 |
| --- | --- | --- |
| `domain` | 20 | 作业跑完并打印完整 JSON 报告后按设计非零退出（lc3d 物理审计 `accepted=false`） |
| `environment` | 7 | `No module named`、`command not found`、`GLIBC_2.38 not found` |
| `code` | 3 | `TypeError: StructuredP1Sampler.__call__`、测试套件 `1 failed, 69 passed` |
| `input` | 3 | `FileNotFoundError: Stage 2A solver_data.npz not found`（上游没跑）、`KeyError: 'anchor_pretilt_deg'`（数据缺字段） |
| `parameter` | 2 | `argument --seed: invalid int value`、`wallSeconds must be <=240` |
| `unknown` | 5 | 无退出码或日志末尾无判定性证据；保持 `unknown`，不回填猜测 |

结论：「失败率」不能直接当健康度读——一半的失败是仿真按设计拒绝了配置，不是工具或环境故障。
这条分布也是 `rcf report` 与批量扫描顺序的依据。

## 4. 通用技术经验与验证边界

- SSH 脚本显式交给 bash，不能假设远端登录 shell 是 POSIX shell。
- Slurm 终态不能仅靠查询命令失败或作业从队列消失判定；应结合持久状态与可信回执。
- WSL/Windows 的进程分离、文件映射、编码、登录会话行为需分别验证；模拟不覆盖全部系统配置。
- MPI 启动器需要匹配实际 MPI/PMI/PMIx；在分配到的计算资源里核对 communicator size，
  不能用“启动了多个进程”证明并行正确性。见 [HPC 兼容性说明](hpc-lammps.md)。
- 进度读取必须有界、不跟随链接。Unix 祖先目录可能只有 execute 权限，兼容性回退仍须保留
  no-follow 与根身份约束。时钟差、reporter 重启和截断都不能被当作真实推进。
- 运行态状态、日志和产物用于恢复；不应为整理仓库而删除真实 run 目录或数据库。

## 5. 明确未承诺的能力

- 多用户隔离、沙箱、恶意作业防护、远程文件防篡改、全局 GPU 调度与公平队列；
- 自动后台清理、跨机备份、断点续传、多节点 MPI 普适兼容性；
- 超长运行/长时间排队的稳定性、未登录 Windows 会话、全部 HPC module 组合；
- 任何站点特定软件版本、性能数字、科研数值结果或 CUDA 计算正确性。

进一步边界见 [security.md](security.md)，后续验证建议见 [roadmap.md](roadmap.md)。
本轮没有上传或发布；真实设备验证仍需单独授权，且不把私域运行样本等同于公开兼容性声明。
