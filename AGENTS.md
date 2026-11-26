# Research Compute Fabric：Codex / ProferAI 共用规则

- 从本仓库根目录接手。先读 [README.md](README.md)、[docs/status.md](docs/status.md) 和 [CONTRIBUTING.md](CONTRIBUTING.md)；接入 CLI/MCP 时读 [docs/agent-guide.md](docs/agent-guide.md)。
- 计划不提交任务；提交前核对无阻塞的计划和用户授权。保留配置、调度、GPU 租约、审计与实际运行的边界。没有退出证据时保持未知，不把心跳或阶段结束当成进程退出，更不当成科研验收。
- 除非另有明确授权，不修改生产配置、不重启现有 MCP、不连真实节点。
- 测试优先使用隔离目录和模拟执行器。凭据与用户路径配置留在仓库之外，不提交运行输出和私有客户端数据。已验证结果、限制和下一步更新到仓库状态或对应文档。
