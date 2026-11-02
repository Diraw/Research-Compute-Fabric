# 公开发布前清单

本清单用于审阅当前工作树，不代表已经提交、上传、发布或清理 Git 历史。

## 1. 公开范围与本机私有材料

公开材料包括 README、LICENSE、pyproject/lock、`.github/`、`docs/`、`examples/`，以及经审阅的
`src/`、`tests/`、`tools/`。示例节点与路径都是占位符；可运行入口为 README 的本机快速开始。
不要把某台机器的配置、性能、故障日志或科研数据变成公共案例。

`.gitignore` 额外排除根级 `.context/`、`private/`、四份实际 RCF 配置、`.env*`、临时发布 scratch
与既有运行态目录。这只防止默认 `git add` 误收新文件：**不撤销已跟踪文件，也不清除历史**。
忽略文件保留在本机；不要删除状态库/日志/真实结果来制造“干净”仓库。

## 2. 自动扫描（只读、离线）

```bash
uv run python tools/publication_check.py --source .
uv run python tools/publication_check.py --source . --include-code --json
uv run pytest tests/unit/test_publication_hygiene.py -p no:cacheprovider
```

没有 uv 时把 `uv run python` 换成对应虚拟环境里的 Python（Windows 为
`.venv\Scripts\python.exe`，Linux/macOS 为 `.venv/bin/python`）。

默认检查明确 public allowlist：根级公开文件和 `docs/`、`examples/`、`.github/`；`--include-code`
再扫描 `src/`、`tests/`、`tools/`。不会遍历 `.venv`、运行数据、实际私有配置或 `.git`。
不跟随符号链接/junction；这类公开路径作为风险单独报告。非文本资产仍需人工审查。

检查项包括：已知私有上下文标记、常见个人 home 路径、非文档保留范围的 IP、常见凭据格式。
输出只含路径、行号与类别，不回显匹配内容。退出 1 表示发现需复核项。源码测试中的合成地址/
故意构造的凭据可能触发，需要按测试目的分类，不能为了全绿随意扩大白名单。
检测器不识别任意私人仓库 owner、机构域名、未知 token 格式或研究语义，扫描通过不是无秘密证明。

## 3. 人工审阅与验收

- [ ] 用 `git status --short` 核对未提交修改与未跟踪文件，不 stash/reset 用户工作树。
- [ ] 审阅 `git diff`、将要暂存的文件及二进制资产；禁止把本机配置、SSH 地址或访问日志加入提交。
- [ ] 文档中的项目与路径一致，示例脚本确实存在，使用标准库而不暗藏额外依赖。
- [ ] 运行离线反例回归、本机 quickstart、相关静态检查；把实际结果写入 `docs/status.md`。
- [ ] 若依赖/构建缓存已就绪，用 `tools/release_check.py` 对新 output 目录做离线候选验收。
- [ ] 核对 wheel/sdist 不含私有文件，MIT 文本与元数据一致；不编造个人版权人或 GitHub owner。
- [ ] 跨平台 CI 配置不当作已通过；真实设备测试、安装依赖、提交或上传各需对应授权。

## 4. 提交历史

当前公开候选从全新仓库开始（工作树已初始化，尚未提交）。如果以后把项目导入已有仓库，
或从其他地方复制提交，再单独检查历史 tree、作者/提交者元数据、remote URL 与旧 tag 中
是否含配置、用户名、路径、节点拓扑或邮箱；本清单不授权重写历史。

如果确认曾提交真实凭据，应先在原系统撤销/轮换，不把“删除当前文件”当成解决。
需要清理历史时必须单独授权，并先备份、评估协作者影响；本清单不授权推送或公开发布。
