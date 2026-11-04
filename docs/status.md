# 功能完成度与验收状态

> 更新：2026-11-04。**当前工作树离线集成验收通过**，不是已上传/已发布版本。
> 测试环境：Windows / Python 3.12.3；macOS arm64 / Python 3.12.16（真实 macOS 节点）。
> 原生 Linux 与远端 GitHub CI 未在本轮执行；macOS 已在真实节点复核。
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
| 观测语义（新鲜度、错误生命周期、ssh 诊断） | `runs/manager.py`、`runs/store.py`、`core/runner.py`、`test_observation_truthfulness.py` | 2026-11-04 新增：每次探测确认写 `state_observed_at`，`run list/status` 返回 `observed_age_seconds` 与 `stale`；成功探测清掉旧 `error` 并记 `observe_recovered`；`get_run`/`run status` 顶层暴露最近一条 `diagnosis`；ssh 255 的 detail 保留 stderr；旧库中 `succeeded` 行的陈旧观测错误一次性清除 |
| 产物契约语义 | `runs/manager.py`、`test_planner_and_manager.py` | `command_override` 的 run 不再误报 `contract_violation`，改记 `output_contract_unchecked`；`output_contract=none` 记 `output_contract_unmanaged`；真正的空产物仍标记违规 |
| 复现元数据传递 | `runs/manager.py`、`test_conventions.py`、`test_planner_and_manager.py` | plan 的 git/inputs 完整写入 run；新增 inputs 摘要传递契约测试；未声明 inputs 或非 git 项目保持 warn，不伪造字段 |
| 代码同步与内容校验 | `projects/push.py`、`test_push_integrity.py` | 不宣称消除恶意共享目录并发 TOCTOU |
| 结果回收与存储治理 | `runs/fetch.py`、`storage/`、`test_storage_*.py` | 有界扫描/归属/指纹/守卫；不可当通用备份或垃圾回收器 |
| 进度、ETA、试跑、预算 | `progress/`、`test_progress_*.py`、`test_pilot_*.py` | reporter 是项目证据，不保证工作量或 ETA 真实性/精确性 |
| 源码交付与构建 | `test_delivery.py`、`test_release_check.py`、`tools/release_check.py` | 离线预检已通过，最终候选以 evidence.json 为准 |
| 真实路径集成（opt-in） | `tests/integration/`、`test_readonly_smoke.py`、`test_local_submit.py`、`test_remote_submit.py` | 默认 skip；2026-11-04 只读 smoke 已在两个真实节点通过，隔离 home 的真实本机 60 秒全链路通过；远端提交/cancel 模板已就绪，待授权执行 |
| 公开材料隐私 | `tools/publication_check.py`、`test_publication_hygiene.py` | 启发式，不扫描 Git 历史，不证明无秘密 |

## 3. 最终验收记录

修复涵盖清理副本/真实归属/声明输入、路径链接写入、取消身份、环境权限与路径、试跑后代、
工作量校准与心跳速率、参数保留名、Windows shell 与 Runner 超时；独立复核追加的同 SSH endpoint
别名与原地 reparse 写入也已补回归。当前 MCP 工具数为 31，校准证据版本为 2。

| 项目 | 命令/证据 | 结果 |
| --- | --- | --- |
| 离线全套（Windows） | `uv run --frozen --offline pytest -m "not integration" -o addopts="--strict-markers" -q -p no:cacheprovider` | **1049 passed / 10 skipped**，343.04 秒（2026-11-04） |
| 离线全套（macOS arm64） | `.venv/bin/python -m pytest -m "not integration" -o addopts="--strict-markers" -q -p no:cacheprovider` | **994 passed / 43 skipped**，114.78 秒 |
| 公开材料扫描 | `uv run --frozen --offline python tools/publication_check.py --source . --include-code` | **通过，0 findings**；启发式，不扫描历史 |
| 源码/测试扫描 | 覆盖 src/tests/tools，合成安全测试使用运行时构造数据 | **通过，0 findings** |
| 外部可用示例 | `uv run --frozen --offline python examples/quickstart.py` | **通过**；临时独立 home、本机 CPU 作业，确定性结果核对成功 |
| Ruff | `uv run --frozen --offline ruff check --no-respect-gitignore src tests tools` | **通过** |
| 真实路径集成（2026-11-04） | `RCF_IT=1 ... pytest tests/integration/test_readonly_smoke.py`；`RCF_IT=1 RCF_IT_JOB_SECONDS=60 ... test_local_submit.py` | **通过**；只读 smoke 覆盖两个真实节点；本机 60 秒 plan → submit → observe → results → fetch 全链路通过；远端提交/cancel 默认 skip |
| 真实 Slurm 夹具（2026-11-04） | `tests/fixtures/slurm/`、`test_real_slurm_outputs.py` | **通过**；真实 `sinfo` 的 `%F`/`+` 内存后缀/`(null)`/`infinite` 与 `sacct` 的 `.batch`/`.extern` 行均被覆盖 |
| 离线候选构建 | `uv run --frozen --offline python tools/release_check.py --source . --output .tmp/release-candidate-*` | **通过**；源码清单、wheel/sdist、隔离安装与 31 工具验证；evidence.json 留在本机，不纳入公开源码 |
| 三平台 CI | Windows/Linux/macOS × Python 3.11/3.12 workflow | 已配置不等于已执行；远端结果未声明 |
| 真实设备 | 私域多节点运行样本（Slurm / SSH / WSL / macOS） | 已有运行证据；公开版本不声明特定站点兼容性；取消、存储清理、进度校准仍待真机验收 |

Windows Bash 用例通过 `tests/bash_support.py` 选择 Git Bash；缺少平台能力的 skip 必须保留
具体原因。测试禁止连接真实服务器，不为通过而放宽断言，不安装依赖、不清空用户工作树。

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
