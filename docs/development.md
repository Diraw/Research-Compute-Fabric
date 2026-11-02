# RCF 开发规范（供后续 Agent 阅读）

本文件是给「下一个接手这个仓库的 Agent」看的。先读它，再读代码。

## 1. 先跑起来

```bash
uv sync
uv run pytest -m "not integration" -p no:cacheprovider  # 离线全套，最终统计以 docs/status.md 为准
uv run pytest -n 8 -m "not integration" -p no:cacheprovider  # 同一套测试并行跑（pytest-xdist）
uv run ruff check --no-respect-gitignore src tests
uv build --offline            # 修改后检查 wheel/sdist 内确实包含 src/rcf/runs
uv run rcf --home <某个测试 home> doctor
```

不要在没跑测试的情况下声称改动完成。

## 2. 三条硬规则

### 2.1 外部进程只能从 `core/runner.py` 出去

要执行命令，接受一个 `Runner` 参数并调用 `runner.run(argv, ...)`。
**不要**在业务模块里 `import subprocess`。唯一例外是 `executors/local_exec.py` 用
`spawn_detached`（它要「脱离父进程」，不是「执行并等待」）。

理由：远程命令的引号、超时、错误分类、测试替身全部集中在一个地方。

### 2.2 错误必须是 `RcfError` 子类，带 `code`

```python
raise NotFound("unknown node 'x'", hint="run `rcf node list`", detail={"node_id": "x"})
```

CLI 用它决定退出码；MCP 把它转成 `{"ok": false, "error": {...}}`。
不要 `raise Exception("...")`，也不要 `print` 错误后继续。

### 2.3 规划与执行分离

任何有副作用的操作都要先有一个可审阅的 plan。`submit` 只接受 `plan_id`，
必须 `confirm=true`，必须复查阻塞项。加新功能时保持这个形状。
同一 plan 只允许一次 dispatch；已有 run（包括 unknown / failed）直接返回。
`Store.claim_submission` 用短事务合并认领、GPU 预约和 run 写入；事务内不得执行网络命令。
不确定提交要保留目录 recovery handle 与租约，不得把没有回执视为没有作业。
`scancel`/信号请求不代表取消完成；没有终态证据就不释放租约。

## 3. 代码风格

- 全部 `from __future__ import annotations`，类型标注齐全。
- Pydantic 模型：配置用 `SpecModel`（`extra=forbid`），运行时用 `RuntimeModel`（`extra=ignore`）。
- 路径：远程一律 POSIX 字符串；本地一律 `pathlib.Path`。不要混。
- 远程命令里的路径用 `core.runner.shquote`（它会保留 `~/` 的展开）。
- 时间：UTC，`core.ids.now_utc()` / `iso()`。
- 注释解释「为什么」，不解释「是什么」。

## 4. 加东西的常见场景

### 新增一个资源指标

1. `resources/probes.py::build_probe_script` 加一段 `echo "@@x"` + 只读命令；
2. `resources/parsers.py` 加纯函数解析；
3. `core/models.py::ResourceSnapshot` 加字段；
4. `assemble_snapshot` 里组装 + `Check`；
5. `tests/unit/test_parsers.py` 加夹具测试（用真实输出，不要编造）。

### 新增一个执行器

1. `executors/xxx_exec.py` 实现 `submit/status/logs/cancel`；
2. `factory.py` 加分支；
3. `core/models.py::NodeKind` 加枚举值（注意 `_consistency` 校验）；
4. 端到端测试：优先用「假 CLI + 真 bash」的方式（参考 `tests/fixtures/fake_slurm.py`），
   而不是 mock 掉自己的代码。

### Windows + WSL 长作业

- 节点仍是 `kind = "ssh"`，用 `[nodes.<id>.windows_task]` 选择 `WindowsTaskExecutor`；
  不要新增 NodeKind，否则探测、push、fetch、环境管理都要分叉。
- Task Scheduler 的 action 必须是 `cmd.exe /c <launcher.cmd>`，不能直接 `wsl.exe`；
  发行版名在 `.cmd` 中不加引号，否则会 `WSL_E_DISTRO_NOT_FOUND`。
- 运行文件仍写在 WSL 的 `work_root`；Windows 启动器路径从 `windows_work_root` 或
  `scp_path_map` 推导，不能用 `\` 硬编码在 Python 字符串里。
- 计划任务无触发器、手动 `Start-ScheduledTask`、终态删除；测试用临时 `run_id` 和
  合成任务名，测试后必须 `Unregister-ScheduledTask` 与删除临时 run 目录。

### 新增一个 MCP 工具

1. `RcfService` 加方法（返回 JSON 友好数据）；
2. `mcp/server.py` 加 `async def compute_xxx` 并注册到 `handlers`；
3. `TOOL_SPECS` 加一条（含 CLI 路径）；
4. `cli/main.py` 加对应命令（否则 `test_mcp_tool_specs_match_cli_and_service` 会失败）。

## 5. 测试与发布约定

- `tests/unit/`：不碰网络、不碰真实 SSH；用 `ScriptedRunner` 和假快照。
- `tests/e2e/`：可以用真 bash + 假 host（`fixtures/fake_ssh.py`），但不得连真实服务器。
- 真实设备测试必需 `@pytest.mark.integration`，默认不执行。
- 临时测试 Git 仓库的 commit 必须 `-c commit.gpgsign=false`，不继承个人 GPG 签名/交互设置。
- 不许为了让测试通过而放宽断言（例如把 `== 0` 改成 `!= 1`）。
- Windows 统一经 `tests/bash_support.py` 定位 Git Bash，不用 `shutil.which("bash")` 误选 WSL launcher。
  `RCF_TEST_BASH` 可显式指定；缺少合适解释器只跳过 Bash 相关用例。
- `.gitignore` 的运行态目录必须限定根路径（`/runs/`），不能把同名源码目录忽略。
  新增源码遗漏由 `test_delivery.py` 用空临时 Git index 检测，避免“已 tracked”掩盖问题。
- 本轮依赖使用 MCP 2.x `MCPServer` API，声明下限为 2.3.0；更新依赖与 lock 一致。

### 5.1 存储治理开发规则

- `storage/fs.py` 是唯一直接枚举/删除科研文件的层；业务层只传根、具体路径和完整指纹。
  不要在 `storage/manager.py` 之外拼 `rm`/`find -delete`，也不要跟随链接或跨挂载点。
- 扫描只读且有界；`complete=false` 时不得生成可执行清理计划。失败/超时要显式返回，
  不能把权限错误当作空目录。
- 所有权必须有回执或显式登记；配置路径不等于所有权。原始数据、pin、活动/unknown 任务、
  有效计划、最近成功 run、唯一未验证副本默认保护。
- 清理是两阶段：`plan` 保存具体文件与指纹，`apply` 原子认领、获取路径守卫、重新校验后
  才执行。删除回执丢失时保留 `unknown` 与守卫；`--resume` 只继续原清单。
- 任何删除相关测试只用临时目录/假 SSH；不得扫描或删除真实科研目录。新增/修改清理逻辑
  必须同时覆盖：外部改动、链接/根替换、并发 submit/push/fetch、计划过期、重复执行和
  中断恢复。
- 清理执行必须持 `execution_lock`；claim/update 携带 owner token，目标 delete 必须传
  `prepare_delete` 返回的 operation（generation）。禁止仅凭时间/PID/guard 缺失接管。
  `BaseException` 与 unknown 写库失败都必须保留 guard；reconcile 只 fence/inspect，不删文件。

### 5.2 进度 / ETA 开发规则

- **不得伪造 ETA**。ETA 只能来自 `progress/estimator.py` 的实测推进；没有可信样本就返回
  `eta_seconds=None` 并给原因。`time_minutes`、`runtime_budget_seconds`、心跳间隔都不是 ETA。
- **心跳 ≠ 推进**。`heartbeat()` 只证明 reporter 活着；`completed` 不前进就是 `stalled`。
  重启/换 session/换 phase/counter reset 必须隔离速率历史，不能跨段外推。
- **有界采集**。`progress/collector.py` 只读尾部、不跟链接；截断/损坏显式标记。
  截断可以给局部 ETA，但**绝不能**用于正式校准。
- **正式预算必须有匹配证据**。负载 key 覆盖参数/代码 commit+dirty/环境/节点/输入；
  改任何一项都重新试跑。脏工作区或无 commit 不得过预算门禁。
- **试跑必须显式且独立**。`pilot_seconds` 只能来自请求/CLI `--pilot-seconds`，不能自动派生；
  硬限制 1..300 秒，watchdog 到期退出码 124；正式任务永不被 pilot 包裹。
- **校准只接受终态真实推进**：成功，或试跑被 124 终止且消耗至少一半时限；单 session、
  单 phase、`total` 匹配、至少两次有效推进、速率比 ≤4。校准带 `evidence_at`，重读旧运行
  不得续期或覆盖更新证据。
- **预算只告警/阻止新计划**，绝不自动 `scancel`/杀正式作业。自动终止只发生在显式试跑。
- **预算时钟用 executor 的 `started_at`**：Slurm 排队时间不能算运行时间；终态优先用
  executor 的 `finished_at` 冻结（缺失才回退控制端观察时间），避免轮询延迟放大运行时。
  `run progress` 必须是只读且先刷新一次状态，否则作业结束后会显示 cached running/stale。节点/控制端时钟差用首个样本 `timestamp - elapsed_seconds` 估计并写
  `clock_skew`，只归一化年龄与起点，不改控制端预算时间。reporter 重启或 tail 截断后新样本
  会低估偏移，必须复用本 run 已观察到的较大偏移；若历史偏移会让最新样本落在未来则拒绝。
- **试跑解释器不能用裸 `python3`**：从 planned argv 解析真实 Python（跳过 `mpirun`/`srun`），
  计算节点可能没有裸 `python3`。POSIX 清理先回收进程组 leader 再 KILL（macOS zombie 会 EPERM）。
- **有界等待**：`run wait` 上限 `min(60, max_wait_seconds)`，共享绝对 deadline；
  `timeout_seconds=0` 只读缓存，不做任何探测；超时不是失败。
- 测试不得连真实节点。进度闭环用临时 Git 仓库 + 真实本地子进程；临时仓库 commit 必须
  `-c commit.gpgsign=false`。测试可以显式构造校准 payload 验证门禁，但不得把“能写进去”
  当成生产证据；端到端必须跑一次真实试跑 → 124 → 校准 → 正式预算的闭环。

### 5.3 发布候选验收

```bash
uv run python tools/release_check.py --source . --output .tmp/release-candidate-unique
```

output 必须是新目录，不允许覆盖已有 output 目录。当前运行依赖及 hatchling/uv build 缓存须预先就绪；
该命令不联网，包含完整 runtime 源码比对、隔离 venv 安装、CLI/MCP 验证与 evidence.json。
`.github/workflows/ci.yml` 配置 Windows/Linux/macOS × 3.11/3.12；新增平台专用用例必须有
准确的 skip 原因。仅配置 workflow 不得宣称远端 CI 已通过。
不要为制造 clean tree 而 stash/commit 用户修改；候选产物可从当前工作树快照构建，正式 Git 发布需单独授权。

### 5.4 公开材料与隐私

- `tools/publication_check.py --source .` 只扫描明确公开 allowlist；加 `--include-code` 才审查源码、测试与工具。
  不遍历 `.venv`、真实 RCF home、运行数据或 Git 历史；输出只包含类别与位置，不能回显疑似凭据。
- 回归使用 `tests/unit/test_publication_hygiene.py`，覆盖隐私反例、私有目录隔离、链接与 Git ignore。
- `examples/quickstart.py` 使用当前解释器与临时 home，运行 `examples/demo/compute.py` 的标准库 CPU 小任务；
  不装依赖、不连远端、保留临时证据，不把它当作进度校准或设备兼容性验证。
- 不把本机路径、实际用户名、SSH 别名、私有 Git remote、科研案例与实测性能写进公开示例。
  最终测试数字仅在实际集成验收后更新 `docs/status.md`，不沿用旧记录。
- `.gitignore` 不会清除历史/已跟踪内容。Git 历史清理、提交与上传分别需要单独授权；完整清单见 `docs/publication.md`。

## 6. 文档约定

改动以下内容时必须同步文档：

| 改了什么 | 更新 |
| --- | --- |
| 工具、CLI 命令 | `docs/agent-guide.md`、`README.md` |
| 安全机制/边界 | `docs/security.md` |
| 完成度 | `docs/status.md`（唯一可信来源） |
| 架构/契约 | `docs/architecture.md` |

## 7. Windows 上的进程创建（踩过的坑）

- **不要用 `DETACHED_PROCESS`（0x8）启动子进程。** 在 ConPTY 宿主（git-bash / Electron）下，
  实测它会给子进程分配一个**可见**控制台窗口，抢走用户焦点（`GetConsoleWindow()` 返回非 0，
  `IsWindowVisible` 为真）。用 `CREATE_NO_WINDOW`（0x08000000）：子进程拿到的是一个**不可见**控制台，
  它自己的子进程也继承同一个，所以作业不会弹窗；同时子进程仍然能在父进程退出后继续运行。
- 所有 `subprocess` 调用都应该带上这个 flag。`core/runner.py` 已经统一处理，
  但写测试夹具或新执行器时容易忘。
- 回归测试：`tests/unit/test_runner.py`（断言 flag）与
  `tests/e2e/test_no_console_window.py`（真实跑一个作业，断言 `GetConsoleWindow() == 0`）。

## 8. 不要做的事

- 不要为了让 demo 好看而伪造真实集群结果。
- 不要在未授权的情况下连接真实设备、安装依赖、提交真实计算。
- 不要把 `StrictHostKeyChecking=no` 加回去「图省事」。
- 不要把 `plan` 和 `submit` 合并成一个工具。
- 不要在没有测试的情况下改 `executors/scripts.py` 的生成逻辑——它是断线恢复的基础。
- 不要在 Windows 上用 `DETACHED_PROCESS` 启动子进程（会弹控制台窗口，见第 7 节）。
