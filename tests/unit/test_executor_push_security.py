"""Real filesystem and shell regressions; no live hosts or important data."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from conftest import local_node, posix_fs, to_win

from rcf.core.config import Config
from rcf.core.errors import RcfError
from rcf.core.ids import now_utc
from rcf.core.models import JobHandle, JobSpec, PlannedCommand, ProjectSpec, ResourceRequirements, SlurmSpec
from rcf.core.runner import SubprocessRunner
from rcf.executors.scripts import cancel_command, run_script
from rcf.executors.slurm_exec import build_sbatch_script
from rcf.projects.push import _SAFE_EXTRACT, ProjectPusher


def _windows_junction_writer(path, target):
    """Open an actual reparse writer; no shell or mocked sharing checks."""
    import ctypes
    import struct
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x40000000, 0x7, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        return ctypes.get_last_error()
    try:
        substitute = ('\\??\\' + str(target)).encode('utf-16-le')
        display = str(target).encode('utf-16-le')
        names = substitute + b'\x00\x00' + display + b'\x00\x00'
        payload = struct.pack('<IHHHHHH', 0xA0000003, 8 + len(names), 0,
                              0, len(substitute), len(substitute) + 2, len(display)) + names
        buffer = ctypes.create_string_buffer(payload)
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x000900A4, buffer, len(payload),
                                      None, 0, ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return 0
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != 'nt', reason='real Win32 reparse and sharing semantics')
def test_junction_writer_fixture_sets_real_reparse_point(tmp_path):
    directory, outside = tmp_path / 'directory', tmp_path / 'outside'
    directory.mkdir()
    outside.mkdir()
    (outside / 'value').write_bytes(b'EXTERNAL')
    try:
        assert _windows_junction_writer(directory, outside) == 0
        assert directory.lstat().st_file_attributes & 0x400
        assert (directory / 'value').read_bytes() == b'EXTERNAL'
    finally:
        if directory.lstat().st_file_attributes & 0x400:
            directory.rmdir()


@pytest.mark.skipif(os.name != 'nt', reason='real Win32 reparse and sharing semantics')
@pytest.mark.parametrize('component', ['root', 'nested'])
def test_extract_pin_blocks_in_place_junction_write(tmp_path, component):
    import io
    import tarfile

    root, outside = tmp_path / 'target', tmp_path / 'outside'
    root.mkdir()
    if component == 'nested':
        (root / 'nested').mkdir()
    outside.mkdir()
    (outside / 'value').write_bytes(b'EXTERNAL')
    relative = 'value' if component == 'root' else 'nested/value'
    archive = tmp_path / 'payload.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        entry = tarfile.TarInfo(relative)
        entry.size = 3
        tar.addfile(entry, io.BytesIO(b'NEW'))
    namespace = {}
    exec(_SAFE_EXTRACT, namespace)
    original = namespace['directory']
    attempted = []
    pinned = root if component == 'root' else root / 'nested'

    def attack_after_pin(*args):
        parent, fds = original(*args)
        try:
            attempted.append(_windows_junction_writer(pinned, outside))
        except BaseException:
            for fd in reversed(fds):
                os.close(fd)
            raise
        return parent, fds

    namespace['directory'] = attack_after_pin
    try:
        namespace['extract'](str(root), str(archive), [relative])
        assert (outside / 'value').read_bytes() == b'EXTERNAL'
        assert attempted == [32], 'pinned ancestor accepted a write-capable handle'
        assert (root / relative).read_bytes() == b'NEW'
    finally:
        # Remove only the temporary junction itself, never its target contents.
        if pinned.lstat().st_file_attributes & 0x400:
            pinned.rmdir()


@pytest.mark.skipif(os.name != 'nt', reason='Windows directory pin and child replacement semantics')
def test_strict_directory_pin_still_allows_child_file_replacement(tmp_path):
    namespace = {}
    exec(_SAFE_EXTRACT, namespace)
    directory = tmp_path / 'target'
    directory.mkdir()
    child, temporary = directory / 'value', directory / 'new'
    outside = tmp_path / 'outside'
    outside.mkdir()
    fd = namespace['pin_windows_directory'](str(directory))
    try:
        assert _windows_junction_writer(directory, outside) == 32
        child.write_bytes(b'OLD')
        temporary.write_bytes(b'NEW')
        namespace['replace_windows_file'](str(temporary), child.name, fd)
        assert child.read_bytes() == b'NEW'
    finally:
        os.close(fd)
        if directory.lstat().st_file_attributes & 0x400:
            directory.rmdir()
    directory.rename(tmp_path / 'released')


def test_local_push_replaces_hardlink_without_changing_external_inode(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'EXTERNAL')
    (source / 'value').write_bytes(b'NEW')
    os.link(outside, target / 'value')
    os.utime(target / 'value', (1000000000, 1000000000))
    pusher = ProjectPusher(SimpleNamespace(home=tmp_path / 'controller'), SubprocessRunner())
    spec = ProjectSpec(id='demo', paths={'source': str(source), 'target': str(target)})
    plan = pusher.plan(spec, local_node('target'), source=local_node('source'))
    report = pusher.apply(spec, local_node('target'), plan, source=local_node('source'))
    assert report.verified
    assert (target / 'value').read_bytes() == b'NEW'
    assert outside.read_bytes() == b'EXTERNAL'
    assert not os.path.samefile(outside, target / 'value')


@pytest.mark.parametrize('late', [False, True])
@pytest.mark.parametrize('kind', ['nested', 'root'])
def test_remote_push_rejects_nested_directory_symlink(slurm_home, ssh_sandbox, tmp_path, monkeypatch, late, kind):
    source, target, outside = (tmp_path / p for p in ('source', 'target', 'outside'))
    (source / 'nested').mkdir(parents=True)
    (source / 'nested' / 'value').write_bytes(b'NEW')
    target.mkdir()
    outside.mkdir()
    (outside / 'value').write_bytes(b'EXTERNAL')
    def link():
        try:
            if kind == 'root':
                target.rmdir()
                target.symlink_to(outside, target_is_directory=True)
            else:
                (target / 'nested').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f'symlinks unavailable: {exc}')
    if not late:
        link()
    config = Config.load(slurm_home)
    node = config.nodes['lab-hpc']
    pusher = ProjectPusher(config, SubprocessRunner())
    spec = ProjectSpec(id='demo', paths={'source': str(source), node.id: posix_fs(target)})
    # fake_scp only implements downloads; copy upload bytes, leaving all
    # generated extraction and validation code real on the fake SSH host.
    import shutil

    from rcf.executors.ssh import SshTransport
    monkeypatch.setattr(SshTransport, 'push_file', lambda self, src, dst, **kw: shutil.copyfile(src, to_win(dst)))
    plan = pusher.plan(spec, node, source=local_node('source'))
    if late:
        original = pusher._pack_file
        def pack(*args):
            result = original(*args)
            link()
            return result
        monkeypatch.setattr(pusher, '_pack_file', pack)
    with pytest.raises(RcfError, match='(?i)link'):
        pusher.apply(spec, node, plan, source=local_node('source'))
    assert (outside / 'value').read_bytes() == b'EXTERNAL'
    assert sorted(path.name for path in outside.iterdir()) == ['value']


@pytest.mark.parametrize('backend', ['ssh', 'slurm'])
@pytest.mark.parametrize('program,code', [
    ("printf 'FIRST_OUT\\n'; printf 'FIRST_ERR\\n' >&2; printf 'LAST_OUT\\n'; false", 1),
    ("printf 'FIRST_OUT\\n' | cat && printf 'LAST_OUT\\n' || false", 0),
])
def test_entire_shell_program_is_redirected(tmp_path, bash_binary, backend, program, code):
    run_dir = tmp_path / backend
    run_dir.mkdir()
    command = PlannedCommand(argv=[program], shell=True, workdir=posix_fs(tmp_path))
    if backend == 'ssh':
        script = run_script(run_id='r', run_dir=posix_fs(run_dir), command=command, workdir=command.workdir)
    else:
        job = JobSpec(run_id='r', resources=ResourceRequirements(), command=command, run_dir=posix_fs(run_dir), log_path=posix_fs(run_dir / 'run.log'))
        script = build_sbatch_script(job=job, run_dir=job.run_dir, slurm=SlurmSpec())
    result = SubprocessRunner().run([bash_binary, '-s'], stdin=script, timeout=30)
    assert result.stdout == ''
    assert result.stderr == ''
    assert result.returncode == code
    assert (run_dir / 'exit_code').read_text().strip() == str(code)
    # Ignore the command preview header when checking actual output.
    text = (run_dir / 'run.log').read_text().splitlines()
    assert text.count('FIRST_OUT') == 1
    assert text.count('LAST_OUT') == 1
    if 'FIRST_ERR' in program:
        assert text.count('FIRST_ERR') == 1


@pytest.mark.parametrize('marker', ['missing', 'forged-start', 'other-run'])
def test_cancel_refuses_live_unrelated_process_before_any_signal(tmp_path, bash_binary, marker):
    run_dir = tmp_path / 'stale-run'
    run_dir.mkdir()
    # Same Bash session makes this meaningful under MSYS as well as Linux.
    # The signal wrapper records all destructive signals; kill -0 remains real.
    script = f'''sleep 30 &
victim=$!
trap 'builtin kill "$victim" 2>/dev/null; wait "$victim" 2>/dev/null' EXIT
printf '%s\\n' "$victim" > {posix_fs(run_dir)}/pid
printf '%s\\n' '999999999999' > {posix_fs(run_dir)}/pid_start
kill() {{ case "$1" in -0) builtin kill "$@" ;; *) printf 'UNSAFE_SIGNAL=%s\\n' "$*"; return 0 ;; esac; }}
{cancel_command(posix_fs(run_dir))}
'''
    if marker == 'missing':
        script = script.replace("printf '%s\\n' '999999999999' > " + posix_fs(run_dir) + '/pid_start', ':')
    elif marker == 'other-run':
        script = script.replace("'999999999999'", '"$(sed -E \'s/.*\\) //\' /proc/$victim/stat | awk \'{print $20}\')"')
    result = SubprocessRunner().run([bash_binary, '-s'], stdin=script, timeout=30)
    assert 'UNSAFE_SIGNAL=' not in result.stdout
    assert 'state=CANCELLED' not in result.stdout
    assert 'cancelled=identity_' in result.stdout


def test_identity_refusal_stays_unknown_even_if_status_reports_lost(monkeypatch):
    from rcf.core.models import JobStatus, NodeSpec, RunState
    from rcf.core.runner import CommandResult
    from rcf.executors.ssh_exec import SshExecutor
    node = NodeSpec(id='n', kind='ssh', ssh_alias='fake')
    transport = SimpleNamespace(alias='fake', run=lambda *a, **kw: CommandResult((), 0, stdout='cancelled=identity_mismatch\nstate=UNKNOWN\n'))
    ex = SshExecutor(node, transport)
    monkeypatch.setattr(ex, 'status', lambda h: JobStatus(state=RunState.LOST, observed_at=now_utc()))
    assert ex.cancel(JobHandle(kind='ssh', node_id='n', workdir='/old')).state == RunState.UNKNOWN
