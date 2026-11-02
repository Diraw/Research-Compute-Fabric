"""SSH argv construction, failure classification and generated scripts."""

from __future__ import annotations

import pytest

from rcf.core.errors import AuthFailed, ExecutorError, HostKeyRejected, HostUnreachable
from rcf.core.models import JobSpec, NodeSpec, PlannedCommand, ResourceRequirements
from rcf.core.runner import CommandResult, ScriptedRunner, classify_ssh_failure, shquote
from rcf.executors.scripts import (
    STATUS_PROBE,
    bootstrap_command,
    cancel_command,
    run_script,
)
from rcf.executors.slurm_exec import build_sbatch_script, format_time_limit
from rcf.executors.ssh import SshTransport


def transport(**kwargs) -> SshTransport:
    return SshTransport(alias="lab-hpc", runner=ScriptedRunner(), **kwargs)


def test_ssh_argv_is_batch_mode_and_never_weakens_host_keys():
    argv = transport().argv()
    joined = " ".join(argv)
    assert argv[0] == "ssh"
    assert "-T" in argv
    assert "BatchMode=yes" in argv
    assert "ConnectTimeout=10" in argv
    assert "StrictHostKeyChecking" not in joined
    assert argv[-2:] == ["lab-hpc", "bash -s"]


def test_alias_is_separated_from_options():
    argv = transport().argv()
    assert argv[-3] == "--"


def test_alias_starting_with_dash_is_rejected():
    with pytest.raises(ExecutorError):
        SshTransport(alias="-oProxyCommand=evil", runner=ScriptedRunner())


def test_from_node_uses_node_options():
    node = NodeSpec(
        id="ws",
        kind="ssh",
        ssh_alias="ws-alias",
        ssh={"connect_timeout": 42, "server_alive_interval": 5},
    )
    built = SshTransport.from_node(node, ScriptedRunner())
    assert built.alias == "ws-alias"
    assert "ConnectTimeout=42" in " ".join(built.argv())
    assert "ServerAliveInterval=5" in " ".join(built.argv())


def test_run_returns_remote_nonzero_without_raising():
    runner = ScriptedRunner([CommandResult(argv=(), returncode=1, stdout="", stderr="not found")])
    result = SshTransport(alias="h", runner=runner).run("command -v nope")
    assert result.returncode == 1


def test_run_raises_on_transport_failure():
    runner = ScriptedRunner(
        [CommandResult(argv=(), returncode=255, stdout="", stderr="Permission denied (publickey).")]
    )
    with pytest.raises(AuthFailed):
        SshTransport(alias="h", runner=runner).run("true")


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("Host key verification failed.", HostKeyRejected),
        ("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!", HostKeyRejected),
        ("ssh: connect to host h port 22: Connection refused", HostUnreachable),
        ("ssh: Could not resolve hostname h", HostUnreachable),
        ("Permission denied (publickey).", AuthFailed),
    ],
)
def test_failure_classification(stderr, expected):
    result = CommandResult(argv=(), returncode=255, stderr=stderr)
    with pytest.raises(expected):
        raise classify_ssh_failure(result, alias="h")


def test_timeout_is_classified_as_unreachable():
    result = CommandResult(argv=(), returncode=124, timed_out=True)
    with pytest.raises(HostUnreachable):
        raise classify_ssh_failure(result, alias="h")


def test_write_file_command_is_base64_and_atomic():
    transport_ = transport()
    command = transport_.write_file_command("/home/me/rcf/run.json", '{"a": "b c"}')
    assert "base64 -d" in command
    assert "mkdir -p" in command
    assert "mv " in command
    assert '{"a"' not in command  # content is never inlined raw


def test_shquote_keeps_tilde_expandable():
    assert shquote("~/rcf/runs/x") == "~/rcf/runs/x"
    assert shquote("~/rcf/a b") == "~/'rcf/a b'"
    assert shquote("/tmp/a b") == "'/tmp/a b'"


def _command() -> PlannedCommand:
    return PlannedCommand(
        argv=["python", "-m", "train", "--name", "two words"],
        workdir="/home/me/proj",
        env={"RCF_RUN_ID": "run_1"},
        preamble=["module load cuda"],
    )


def test_run_script_writes_state_atomically_and_quotes_arguments():
    script = run_script(
        run_id="run_1",
        run_dir="/home/me/rcf/runs/run_1",
        command=_command(),
        workdir="/home/me/proj",
    )
    assert 'printf \'%s\\n\' $$ > "$RUN_DIR/pid"' in script
    assert "python -m train --name 'two words'" in script
    assert ".exit_code.tmp" in script and "exit_code" in script
    assert "finished_at" in script
    assert "module load cuda" in script
    assert "cd /home/me/proj" in script


def test_run_script_without_preamble_records_that_fact():
    script = run_script(
        run_id="r", run_dir="/d", command=PlannedCommand(argv=["true"], workdir="/w"), workdir="/w"
    )
    assert "no environment setup" in script


def test_status_probe_reports_missing_directory_and_pid_marker():
    text = STATUS_PROBE.format(run_dir="/home/me/r")
    assert "state=MISSING" in text
    assert "pid_marker=mismatch" in text
    assert "exit_code=$(cat exit_code" in text


def test_cancel_command_uses_process_group_when_possible():
    text = cancel_command("/home/me/r")
    assert 'pgid=$(ps -o pgid= -p "$pid"' in text
    assert 'kill -TERM -"$pid"' in text
    assert "kill -KILL" in text


def test_bootstrap_detaches_with_setsid_or_nohup():
    text = bootstrap_command("/home/me/r")
    assert "setsid bash run.sh" in text
    assert "nohup bash run.sh" in text
    assert "/dev/null" in text


def test_sbatch_script_contains_expected_directives():
    node = NodeSpec(
        id="hpc",
        kind="slurm",
        ssh_alias="hpc",
        slurm={"default_partition": "gpu", "account": "proj123"},
    )
    job = JobSpec(
        run_id="run_1",
        run_dir="/home/me/rcf/runs/run_1",
        command=PlannedCommand(argv=["python", "train.py"], workdir="/home/me/proj"),
        log_path="/home/me/rcf/runs/run_1/run.log",
        resources=ResourceRequirements(gpus=2, cpus=8, memory_gb=32, time_minutes=90),
    )
    script = build_sbatch_script(job=job, run_dir=job.run_dir, slurm=node.slurm, partition="gpu")
    assert "#SBATCH --job-name=" in script
    assert "#SBATCH --partition=gpu" in script
    assert "#SBATCH --account=proj123" in script
    assert "#SBATCH --gres=gpu:2" in script
    assert "#SBATCH --cpus-per-task=8" in script
    assert "#SBATCH --mem=32G" in script
    assert "#SBATCH --time=01:30:00" in script
    assert script.index("#SBATCH") < script.index("set -o pipefail")


def test_sbatch_omits_gres_without_gpus():
    node = NodeSpec(id="hpc", kind="slurm", ssh_alias="hpc", slurm={"default_partition": "cpu"})
    job = JobSpec(
        run_id="r",
        run_dir="/d",
        command=PlannedCommand(argv=["true"], workdir="/w"),
        log_path="/d/run.log",
        resources=ResourceRequirements(cpus=2),
    )
    script = build_sbatch_script(job=job, run_dir="/d", slurm=node.slurm, partition="cpu")
    assert "--gres" not in script


def test_time_limit_formatting():
    assert format_time_limit(90) == "01:30:00"
    assert format_time_limit(60 * 24 * 2 + 60 * 3) == "2-03:00:00"


def test_timed_out_result_is_reported():
    result = CommandResult(argv=(), returncode=124, timed_out=True, duration_s=5)
    assert result.timed_out
    assert not result.ok


def test_script_goes_over_stdin_so_the_login_shell_never_parses_it():
    """A fish/csh login shell must not see our POSIX script.

    Regression: `ssh host "<if [ -d x ]; then ... fi>"` was handed to fish and
    failed with "fish: Unknown command", so every launch broke on a real Ubuntu
    workstation.
    """

    runner = ScriptedRunner([CommandResult(argv=(), returncode=0, stdout="ok")])
    script = "if [ -d /tmp/x ]; then echo yes; else echo no; fi"
    SshTransport(alias="h", runner=runner).run(script)

    sent_argv = " ".join(runner.calls[0])
    assert script not in sent_argv
    assert "if [" not in sent_argv
    assert runner.calls[0][-1] == "bash -s"
    assert runner.inputs[0] == script


def test_missing_bash_gets_a_clear_error():
    from rcf.core.errors import ExecutorError

    runner = ScriptedRunner(
        [CommandResult(argv=(), returncode=127, stderr="bash: command not found")]
    )
    with pytest.raises(ExecutorError, match="no bash"):
        SshTransport(alias="h", runner=runner).run_checked("true", what="probe")


def test_scp_paths_translate_for_windows_hosts_running_wsl():
    """A WSL node runs bash in /mnt/e but its scp speaks Windows paths."""

    from rcf.core.models import NodeSpec
    from rcf.core.runner import SubprocessRunner
    from rcf.executors.ssh import SshTransport

    node = NodeSpec(
        id="kb",
        kind="ssh",
        ssh_alias="kb",
        work_root="/mnt/e/rcf",
        scp_path_map={"/mnt/e": "E:/"},
    )
    transport = SshTransport.from_node(node, SubprocessRunner())
    assert transport._scp_remote("/mnt/e/rcf/runs/x.tar.gz") == "E:/rcf/runs/x.tar.gz"
    assert transport._scp_remote("/mnt/e") == "E:/"
    assert transport._scp_remote("/home/example/file") == "/home/example/file"
