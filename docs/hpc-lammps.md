# HPC 上的 LAMMPS / Python / MPI 兼容性检查

本文保留可复用的检查方法，不提供某台集群的故障记录、私有项目案例、性能数据或安装承诺。
它不是自动安装脚本；版本、module 名称和编译开关必须结合当前 LAMMPS 官方文档与站点政策复核。
任何安装、联网下载、提交测试作业都需要相应授权。

## 1. 先确认执行环境，而不是直接重装

登录节点与计算节点的 PATH、module、glibc、驱动和 MPI 可能不同。只读检查可包括：

```bash
command -v python3
python3 --version
python3 -c "import ssl, sqlite3; print(ssl.OPENSSL_VERSION, sqlite3.sqlite_version)"
command -v cmake
cmake --version
command -v mpiexec
```

计算节点侧的检查应在站点允许的调度器 allocation 内执行，不要直接 SSH 到计算节点。
若 module 加载成功但 PATH 不变，或 Python 扩展缺少共享库，先让管理员确认模块与 ABI，
不要用全局 `LD_LIBRARY_PATH` 掩盖问题。仅有 `lmp` 命令并不意味着 Python 模块和共享库可用。

## 2. Python API 的最小探测

在目标解释器、目标环境中测试，而不是控制端或另一套 Python：

```bash
python3 -c "from lammps import lammps; sim = lammps(cmdargs=['-log','none','-screen','none']); print(sim.version()); sim.close()"
```

若导入失败，区分以下层次：

| 层次 | 检查方向 |
| --- | --- |
| Python 找不到模块 | 解释器、安装前缀与 site-packages 是否一致 |
| 找不到 liblammps | 是否构建共享库，Python 模块是否与共享库配套 |
| 符号/ABI 错误 | glibc、编译器运行库、Python、MPI 与依赖是否来自兼容工具链 |
| GPU/插件错误 | 软件构建选项、驱动、设备分配；不应以 import 成功证明计算正确 |

## 3. 必须自行构建时

优先使用管理员维护的兼容环境；确需用户级构建时，安装到专用、可写、未与生产环境混用的前缀。
审核下载来源、许可证、版本与校验和；不要执行来源不明的脚本，也不要覆盖已有科研环境。

CMake 通常需考虑共享库、Python 包、MPI 与安装前缀，但各版本选项会变化。下面只是
**需替换的构建结构**，不是已在当前版本验证的配方：

```bash
cmake -S /path/to/lammps/cmake -B /path/to/new-build \
  -DCMAKE_INSTALL_PREFIX=/home/researcher/opt/lammps \
  -DBUILD_SHARED_LIBS=ON -DBUILD_MPI=ON -DPKG_PYTHON=ON
cmake --build /path/to/new-build --parallel 4
```

不要直接复制旧版本 site-packages、删除已有包目录或强行链接不同来源的库。JPEG/PNG/CURL、
Python 链接与编译器 wrapper 等选项需要按实际诊断决定，不存在适用于所有集群的“必须关闭”清单。
安装方式以该版本官方说明为准。

## 4. MPI：进程数不等于 communicator size

MPI 库、启动器与调度器的 PMI/PMIx 集成必须匹配。部分不带相应集成的构建可能启动多个
独立进程，却每个都只看到 size=1；这不一定报错。用合成程序在分配资源内核对：

```bash
mpiexec -n 4 python3 -c "from mpi4py import MPI; print(MPI.COMM_WORLD.rank, MPI.COMM_WORLD.size)"
```

只有在站点允许且 mpi4py 与实际 MPI ABI 匹配时执行。期望四个不同 rank、同一 size=4；
随后还需运行应用自己的正确性检查。不能把这个探测当成性能或多节点兼容性保证。
RCF 的 `{launcher}` 或环境 `mpi_launcher` 仍需要按站点配置验证。

## 5. 在 RCF 中登记已有环境

以下字段仅用于说明映射，所有路径与节点 ID 都是占位符。RCF 环境 ID 可以不同于实际前缀名：

```toml
[environments.mpi-python]
kind = "conda"
python = "python3"
conda_root = "/home/researcher/miniforge3"
path = { "cluster" = "/home/researcher/envs/mpi-python" }
mpi_launcher = "mpiexec -n {tasks}"
```

不要把 `managed=true` 当作探测开关：它表示把已有前缀的变更权限交给 RCF，需明确授权。
先只读 verify，再用合成输入、短 walltime、明确产物目录进行授权冒烟。数值正确性、
科研结果复现与性能测量应由应用自己的测试协议验证，不由 RCF 的成功退出替代。
