"""Exercise the fixed contracts over fake SSH, with real Bash and files."""
from __future__ import annotations

import os
import shutil

import pytest
from conftest import local_node, posix_fs, to_win

from rcf.core.config import Config
from rcf.core.models import PlannedCommand, ProjectSpec
from rcf.core.runner import SubprocessRunner
from rcf.executors.scripts import cancel_command, run_script
from rcf.executors.ssh import SshTransport
from rcf.projects.push import ProjectPusher

pytestmark = pytest.mark.e2e


def test_remote_push_replaces_hardlinks_and_creates_nested_files(slurm_home, ssh_sandbox, tmp_path, monkeypatch):
    config = Config.load(slurm_home)
    node = config.nodes['lab-hpc']
    source, target = tmp_path / 'source', ssh_sandbox.host / 'deployment'
    (source / 'nested').mkdir(parents=True)
    target.mkdir()
    (source / 'nested' / 'value').write_bytes(b'nested payload')
    (source / 'value').write_bytes(b'NEW')
    outside = tmp_path / 'outside'
    outside.write_bytes(b'EXTERNAL')
    os.link(outside, target / 'value')
    os.utime(target / 'value', (1000000000, 1000000000))
    monkeypatch.setattr(SshTransport, 'push_file', lambda self, src, dst, **kw: shutil.copyfile(src, to_win(dst)))
    pusher = ProjectPusher(config, SubprocessRunner())
    spec = ProjectSpec(id='demo', paths={'source': str(source), node.id: posix_fs(target)})
    plan = pusher.plan(spec, node, source=local_node('source'))
    report = pusher.apply(spec, node, plan, source=local_node('source'))
    assert report.verified
    assert report.files == 2
    assert (target / 'nested' / 'value').read_bytes() == b'nested payload'
    assert (target / 'value').read_bytes() == b'NEW'
    assert outside.read_bytes() == b'EXTERNAL'
    assert not os.path.samefile(outside, target / 'value')


def test_real_run_identity_accepts_only_its_original_script(tmp_path, bash_binary):
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    command = PlannedCommand(argv=['sleep', '30'], workdir=posix_fs(tmp_path))
    text = run_script(run_id='r', run_dir=posix_fs(run_dir), command=command, workdir=command.workdir)
    (run_dir / 'run.sh').write_text(text, encoding='utf-8', newline='\n')
    # Both launch/cancel live in one Bash session so MSYS has real PID visibility.
    # wait in the background reaps the child, allowing kill -0 disappearance.
    script = f'''cd {posix_fs(run_dir)}
bash run.sh &
child=$!
trap 'builtin kill "$child" 2>/dev/null; wait "$child" 2>/dev/null' EXIT
for i in {{1..100}}; do test -s pid_start && test -s run.log && break; sleep 0.05; done
kill() {{
  case "$1" in
    -0) builtin kill "$@" ;;
    *) printf 'SIGNAL=%s\\n' "$*"; builtin kill "$@" ;;
  esac
}}
{cancel_command(posix_fs(run_dir))}
'''
    result = SubprocessRunner().run([bash_binary, '-s'], stdin=script, timeout=30)
    assert 'SIGNAL=-TERM ' in result.stdout, (result.stdout, result.stderr)
    assert 'cancelled=term' in result.stdout, (result.stdout, result.stderr)
    assert 'state=CANCELLED' in result.stdout


def test_reused_pid_between_term_and_kill_is_not_signalled_again(tmp_path, bash_binary):
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    command = PlannedCommand(argv=['sleep', '30'], workdir=posix_fs(tmp_path))
    (run_dir / 'run.sh').write_text(run_script(run_id='r', run_dir=posix_fs(run_dir), command=command,
                                             workdir=command.workdir), encoding='utf-8', newline='\n')
    # Keep the original live, but change the durable process instance after
    # TERM. Revalidation must refuse escalation for the now different instance.
    # All probes and first identity validation remain real; only signalling is
    # intercepted to avoid killing the fixture and to force the escalation race.
    script = f'''cd {posix_fs(run_dir)}
bash run.sh &
child=$!
trap 'builtin kill "$child" 2>/dev/null; wait "$child" 2>/dev/null' EXIT
for i in {{1..100}}; do test -s pid_start && test -s run.log && break; sleep 0.05; done
kill() {{ case "$1" in -0) builtin kill "$@" ;; -TERM) echo TERM_REQUEST; echo changed > pid_start ;; *) echo UNSAFE_KILL ;; esac; }}
sleep() {{ :; }}
{cancel_command(posix_fs(run_dir))}
'''
    result = SubprocessRunner().run([bash_binary, '-s'], stdin=script, timeout=30)
    assert 'TERM_REQUEST' in result.stdout, (result.stdout, result.stderr)
    assert 'UNSAFE_KILL' not in result.stdout
    assert 'cancelled=identity_changed' in result.stdout
    assert 'state=CANCELLED' not in result.stdout
