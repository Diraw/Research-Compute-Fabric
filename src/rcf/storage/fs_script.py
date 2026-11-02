"""Self-contained Python 3.6-compatible helper, deliberately kept as source.

Do not import project modules here: the exact same program is sent to SSH hosts.
"""
from __future__ import annotations

SCRIPT = r'''
import base64
import datetime
import errno
import hashlib
import json
import ntpath
import os
import stat
import sys
import time

REQUEST = None


def relative(path):
    if (not isinstance(path, str) or not path or '\x00' in path or '\\' in path
            or path.startswith('/') or ntpath.splitdrive(path)[0]
            or any(p in ('', '.', '..') for p in path.split('/'))):
        raise ValueError('invalid relative path')
    if os.name == 'nt':
        for part in path.split('/'):
            stem = part.split('.')[0].upper()
            if (':' in part or part.endswith((' ', '.'))
                    or stem in ('CON', 'PRN', 'AUX', 'NUL')
                    or stem in ['COM' + str(n) for n in range(1, 10)]
                    or stem in ['LPT' + str(n) for n in range(1, 10)]):
                raise ValueError('ambiguous Windows path/device/alternate stream blocked')
    return path


def link(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


def identity(st):
    return [str(st.st_dev), str(st.st_ino)]


def signature(st):
    # Windows CRT fstat and path lstat disagree on ctime semantics on Python
    # 3.12; identity/mtime/size/nlink/mode and the content hash remain checked.
    return (str(st.st_dev), str(st.st_ino), st.st_size, st.st_mtime_ns,
            st.st_ctime_ns if os.name != 'nt' else 0, st.st_nlink, st.st_mode)


def check_time(deadline):
    if time.monotonic() >= deadline:
        raise RuntimeError('time budget exceeded')


def record(path, st):
    kind = 'symlink' if link(st) else ('dir' if stat.S_ISDIR(st.st_mode) else 'file')
    if kind == 'file' and not stat.S_ISREG(st.st_mode):
        raise ValueError('unsupported special file')
    blocks = getattr(st, 'st_blocks', None)
    return dict(path=path, kind=kind, bytes=st.st_size, mtime_ns=st.st_mtime_ns,
                device=str(st.st_dev), inode=str(st.st_ino), nlink=st.st_nlink,
                allocated_bytes=None if blocks is None else blocks * 512, sha256=None)


if os.name == 'nt':
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    wintypes.LPVOID, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 wintypes.LPVOID, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL

    def win_open(path, directory=False, deleting=False):
        # Deny DELETE and WRITE sharing: ancestors cannot move or acquire
        # reparse metadata, and the verified file cannot acquire new writers.
        access = 0x80000000 | (0x10000 if deleting else 0)
        share = 1
        handle = kernel.CreateFileW(path, access, share, None, 3, 0x02200000, None)
        if handle == wintypes.HANDLE(-1).value:
            error = ctypes.get_last_error()
            if error in (2, 3):
                raise FileNotFoundError(errno.ENOENT, 'missing', path)
            raise OSError(error, 'safe handle open failed', path)
        attrs = (wintypes.DWORD * 2)()
        if not kernel.GetFileInformationByHandleEx(handle, 9, attrs, ctypes.sizeof(attrs)):
            kernel.CloseHandle(handle)
            raise OSError('cannot inspect reparse attributes')
        if attrs[0] & 0x400:
            kernel.CloseHandle(handle)
            raise ValueError('reparse point blocked')
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        return fd


def absolute_parts(path):
    drive, tail = os.path.splitdrive(path)
    base = drive + os.sep
    parts = [p for p in tail.split(os.sep) if p]
    return base, parts


class Scope(object):
    def __init__(self, root, deadline):
        expanded = os.path.expanduser(root)
        if os.name == 'nt':
            if expanded.startswith(('\\\\?\\', '\\\\.\\')):
                raise ValueError('Windows device namespace blocked')
            tail = os.path.splitdrive(expanded)[1]
            for part in tail.replace('\\', '/').split('/'):
                if part:
                    relative(part)
        elif '..' in expanded.split('/'):
            raise ValueError('root traversal blocked')
        self.root = os.path.abspath(expanded)
        self.deadline = deadline
        self.fds = []
        self.chain = []
        self.fd = None
        self.device = None
        self.mounts = set()

    def __enter__(self):
        try:
            base, parts = absolute_parts(self.root)
            if os.name != 'nt':
                if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
                    raise RuntimeError('safe dir_fd/no-follow unavailable')
                fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            else:
                fd = win_open(base, directory=True)
            self.fds.append(fd)
            self.chain.append((base, identity(os.fstat(fd))))
            current = base
            for part in parts:
                check_time(self.deadline)
                current = os.path.join(current, part)
                if os.name == 'nt':
                    fd = win_open(current, directory=True)
                else:
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=fd)
                self.fds.append(fd)
                self.chain.append((current, identity(os.fstat(fd))))
            self.fd = fd
            self.device = os.fstat(fd).st_dev
            # Linux bind mounts may share st_dev. Mountinfo is read-only and
            # decodes kernel escaping, never whitespace-splits user filenames.
            if sys.platform.startswith('linux'):
                with open('/proc/self/mountinfo', 'r') as mounts:
                    for line in mounts:
                        field = line.split(' ')[4]
                        for escaped, literal in [('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')]:
                            field = field.replace(escaped, literal)
                        self.mounts.add(os.path.normpath(field))
            if os.path.normcase(os.path.realpath(self.root)) != os.path.normcase(self.root):
                raise ValueError('root is not canonical or has a link ancestor')
            self.verify()
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds = []

    def __exit__(self, *args):
        self.close()

    def verify(self):
        check_time(self.deadline)
        for path, expected in self.chain:
            st = os.lstat(path)
            if link(st) or identity(st) != expected:
                raise ValueError('root/ancestor identity changed')
        if os.path.normcase(os.path.realpath(self.root)) != os.path.normcase(self.root):
            raise ValueError('root canonical path changed')

    def parent(self, path):
        # Returned descriptors remain live through hashing and deletion.
        relative(path)
        fd = self.fd
        held = []
        ancestors = []
        current = self.root
        try:
            for part in path.split('/')[:-1]:
                check_time(self.deadline)
                current = os.path.join(current, part)
                if current in self.mounts:
                    raise ValueError('mount boundary blocked')
                if os.name == 'nt':
                    child = win_open(current, directory=True)
                else:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                held.append(child)
                st = os.fstat(child)
                if st.st_dev != self.device:
                    raise ValueError('mount boundary blocked')
                ancestors.append(identity(st))
                fd = child
            return fd, held, ancestors
        except BaseException:
            for child in reversed(held):
                os.close(child)
            raise

    def lstat(self, path, parent):
        if os.path.join(self.root, *path.split('/')) in self.mounts:
            raise ValueError('mount boundary blocked')
        if os.name == 'nt':
            return os.lstat(os.path.join(self.root, *path.split('/')))
        return os.stat(path.split('/')[-1], dir_fd=parent, follow_symlinks=False)

    def open_file(self, path, parent, deleting=False):
        if os.name == 'nt':
            return win_open(os.path.join(self.root, *path.split('/')), deleting=deleting)
        return os.open(path.split('/')[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=parent)

    def verify_parent(self, path, ancestors):
        self.verify()
        parent, held, current = self.parent(path)
        try:
            if current != ancestors:
                raise ValueError('parent identity changed')
        finally:
            for child in reversed(held):
                os.close(child)


class OperationStore(object):
    """Pinned metadata + OS lock + fsynced append-only manifest.

    A torn journal fails closed, never resets a generation. A running generation
    cannot be retried: only prepare after its lock is released can fence it.
    """
    def __init__(self, scope, operation_id):
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 1024:
            raise ValueError('invalid operation id')
        self.scope = scope
        self.operation_id = operation_id
        self.key = hashlib.sha256(operation_id.encode('utf-8')).hexdigest()
        self.fds = []
        self.checked = []
        self.locked = False
        self.lock_fd = None
        self.manifest = None
        self.latest = None

    def checked_stat(self, fd, directory=False):
        st = os.fstat(fd)
        if (link(st) or not st.st_ino or st.st_dev != self.scope.device
                or not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode))
                or (not directory and st.st_nlink != 1)):
            raise ValueError('unsafe operation metadata type/identity/hardlink')
        if os.name != 'nt' and (st.st_uid != os.geteuid() or st.st_mode & 0o022):
            raise ValueError('operation metadata must be owned and not group/world writable')
        return st

    def open(self, path, parent, directory=False):
        name = path.split('/')[-1]
        full = os.path.join(self.scope.root, *path.split('/'))
        if full in self.scope.mounts:
            raise ValueError('operation metadata mount blocked')
        if directory:
            try:
                if os.name == 'nt':
                    os.mkdir(full, 0o700)
                else:
                    os.mkdir(name, 0o700, dir_fd=parent)
                    os.fsync(parent)
            except FileExistsError:
                pass
            if os.name == 'nt':
                fd = win_open(full, directory=True)
            else:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        elif os.name == 'nt':
            # OPEN_ALWAYS + OPEN_REPARSE_POINT: inspect the returned object,
            # never follow an unknown reparse point. Deny DELETE sharing so a
            # live lock/manifest cannot be replaced; allow competing lockers.
            handle = kernel.CreateFileW(full, 0xC0000000, 3, None, 4, 0x00200000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise OSError(ctypes.get_last_error(), 'safe metadata open failed')
            attrs = (wintypes.DWORD * 2)()
            if (not kernel.GetFileInformationByHandleEx(handle, 9, attrs, ctypes.sizeof(attrs))
                    or attrs[0] & 0x400):
                kernel.CloseHandle(handle)
                raise ValueError('operation metadata reparse point blocked')
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            except BaseException:
                kernel.CloseHandle(handle)
                raise
        else:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=parent)
            os.fsync(parent)
        self.fds.append(fd)
        st = self.checked_stat(fd, directory)
        current = self.scope.lstat(path, parent)
        if link(current) or identity(st) != identity(current):
            raise ValueError('operation metadata replaced during open')
        self.checked.append((path, parent, fd, directory))
        return fd

    def verify(self):
        self.scope.verify()
        for path, parent, fd, directory in self.checked:
            st = self.checked_stat(fd, directory)
            current = self.scope.lstat(path, parent)
            if link(current) or identity(st) != identity(current):
                raise ValueError('operation metadata identity changed')

    def __enter__(self):
        try:
            parent = self.scope.fd
            for path in ('.rcf-storage', '.rcf-storage/operations'):
                parent = self.open(path, parent, directory=True)
            prefix = '.rcf-storage/operations/' + self.key
            self.lock_fd = self.open(prefix + '.lock', parent)
            # Windows byte-range locks may cover EOF; no pre-lock write (which
            # could itself conflict with a live locker) is needed.
            os.lseek(self.lock_fd, 0, os.SEEK_SET)
            try:
                if os.name == 'nt':
                    msvcrt.locking(self.lock_fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('busy: a live storage helper owns this operation')
            self.locked = True
            self.manifest = self.open(prefix + '.jsonl', parent)
            self.verify()
            size = os.fstat(self.manifest).st_size
            if size > 32 * 1024 * 1024:
                raise ValueError('operation journal size limit; manual recovery required')
            data = b''
            while len(data) < size:
                block = os.read(self.manifest, min(1024 * 1024, size - len(data)))
                if not block:
                    raise ValueError('operation journal shortened')
                data += block
            if data and not data.endswith(b'\n'):
                raise ValueError('torn operation journal; manual recovery required')
            for line in data.splitlines():
                item = json.loads(line.decode('utf-8'))
                generation = item.get('generation')
                if (item.get('operation_id') != self.operation_id or type(generation) is not int
                        or generation < 1 or (self.latest and generation < self.latest['generation'])
                        or item.get('state') not in ('prepared', 'running', 'complete')
                        or item.get('root_identity') != identity(os.fstat(self.scope.fd))
                        or item.get('root_ancestors') != [value for location, value in self.scope.chain]):
                    raise ValueError('invalid operation journal; manual recovery required')
                self.latest = item
            return self
        except BaseException:
            self.__exit__()
            raise

    def save(self, value):
        self.verify()
        value.update(operation_id=self.operation_id,
                     root_identity=identity(os.fstat(self.scope.fd)),
                     root_ancestors=[value for location, value in self.scope.chain])
        payload = (json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n').encode('utf-8')
        # Append-only: a crash either leaves the preceding durable snapshot or
        # an invalid suffix. Never truncate/reset a possibly spent generation.
        os.lseek(self.manifest, 0, os.SEEK_END)
        offset = 0
        while offset < len(payload):
            count = os.write(self.manifest, payload[offset:])
            if not count:
                raise OSError('short operation journal write')
            offset += count
        os.fsync(self.manifest)
        self.latest = value

    def __exit__(self, *args):
        if self.locked:
            if os.name == 'nt':
                os.lseek(self.lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(self.lock_fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            self.locked = False
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds = []


def digest(fd, st, deadline):
    if not stat.S_ISREG(st.st_mode) or link(st):
        raise ValueError('hash requires regular file')
    remaining = st.st_size
    hashed = hashlib.sha256()
    while remaining:
        check_time(deadline)
        block = os.read(fd, min(1024 * 1024, remaining))
        if not block:
            raise ValueError('file shortened during hash')
        hashed.update(block)
        remaining -= len(block)
    check_time(deadline)
    if os.read(fd, 1) or signature(os.fstat(fd)) != signature(st):
        raise ValueError('file changed during hash')
    return hashed.hexdigest()


def inspect_one(scope, path, hashes):
    relative(path)
    if path.split('/')[0].lower() == '.rcf-storage':
        raise ValueError('protected operation metadata')
    parent, held, ancestors = scope.parent(path)
    try:
        st = scope.lstat(path, parent)
        entry = record(path, st)
        if st.st_dev != scope.device:
            raise ValueError('mount boundary blocked')
        if entry['kind'] == 'file' and hashes:
            fd = scope.open_file(path, parent)
            try:
                opened = os.fstat(fd)
                if signature(opened) != signature(st):
                    raise ValueError('file changed before hash')
                entry['sha256'] = digest(fd, opened, scope.deadline)
                scope.verify_parent(path, ancestors)
                if signature(scope.lstat(path, parent)) != signature(opened):
                    raise ValueError('file replaced during hash')
            finally:
                os.close(fd)
        entry['root_identity'] = identity(os.fstat(scope.fd))
        entry['root_ancestors'] = [value for location, value in scope.chain]
        entry['ancestors'] = ancestors
        return entry
    finally:
        for fd in reversed(held):
            os.close(fd)


def error(result, path, exc):
    result['complete'] = False
    result['errors'].append(dict(path=path, reason=str(exc)))


def scan(scope, result, maximum, exclude_names=None):
    excluded = set(exclude_names or ()) | set(['.rcf-storage'])
    # Streaming scandir never materializes a huge directory listing. Python 3.6
    # lacks scandir(fd); fd pseudo-paths preserve no-follow traversal there.
    stack = []

    def iterator(fd, path):
        if os.name == 'nt':
            return os.scandir(path)
        try:
            return os.scandir(fd)
        except TypeError:
            for prefix in ('/proc/self/fd/', '/dev/fd/'):
                location = prefix + str(fd)
                if os.path.exists(location):
                    return os.scandir(location)
            raise RuntimeError('bounded descriptor scandir unavailable')

    def push(fd, relative_path, owned):
        try:
            it = iterator(fd, os.path.join(scope.root, *relative_path.split('/')))
        except BaseException:
            if owned:
                os.close(fd)
            raise
        stack.append((fd, relative_path, it, owned))

    seen = set([tuple(identity(os.fstat(scope.fd)))])
    visited = 0
    try:
        push(scope.fd, '', False)
        while stack:
            check_time(scope.deadline)
            fd, prefix, it, owned = stack[-1]
            try:
                child = next(it)
            except StopIteration:
                it.close()
                if owned:
                    os.close(fd)
                stack.pop()
                continue
            if child.name in excluded or child.name.lower() == '.rcf-storage':
                continue
            if visited >= maximum:
                raise RuntimeError('entry budget exceeded')
            visited += 1
            path = prefix + '/' + child.name if prefix else child.name
            try:
                relative(path)
                # Windows DirEntry caches enumeration metadata without file ID.
                st = os.lstat(os.path.join(scope.root, *path.split('/'))) if os.name == 'nt' else child.stat(follow_symlinks=False)
                entry = record(path, st)
                result['entries'].append(entry)
                if link(st):
                    continue
                if st.st_dev != scope.device or os.path.join(scope.root, *path.split('/')) in scope.mounts:
                    error(result, path, ValueError('mount boundary skipped'))
                    continue
                if entry['kind'] != 'dir':
                    continue
                if tuple(identity(st)) in seen:
                    error(result, path, ValueError('repeated directory identity skipped'))
                    continue
                seen.add(tuple(identity(st)))
                if os.name == 'nt':
                    child_fd = win_open(os.path.join(scope.root, *path.split('/')), directory=True)
                else:
                    child_fd = os.open(child.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                if identity(os.fstat(child_fd)) != identity(st):
                    os.close(child_fd)
                    raise ValueError('directory replaced during scan')
                push(child_fd, path, True)
            except (OSError, ValueError, RuntimeError) as exc:
                error(result, path, exc)
    except (OSError, ValueError, RuntimeError) as exc:
        error(result, '', exc)
    finally:
        for fd, prefix, it, owned in reversed(stack):
            it.close()
            if owned:
                os.close(fd)
    scope.verify()


def protected_root(root):
    root = os.path.normcase(root)
    home = os.path.normcase(os.path.abspath(os.path.expanduser('~')))
    base, parts = absolute_parts(root)
    if root == home or root == os.path.normcase(base):
        return True
    try:
        if os.path.commonpath([root, home]) == root:
            return True
    except ValueError:
        pass
    if os.name == 'nt':
        protected = [os.environ.get(k, '') for k in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData')]
    else:
        protected = ['/bin', '/boot', '/dev', '/etc', '/lib', '/lib64', '/proc', '/sbin', '/sys', '/usr', '/var',
                     '/System', '/Library', '/Applications', '/opt', '/run', '/snap']
    for item in protected:
        if not item:
            continue
        item = os.path.normcase(os.path.abspath(item))
        if root == item or root.startswith(item.rstrip(os.sep) + os.sep):
            return True
    return False


def delete_one(scope, entry):
    path = relative(entry.get('path'))
    if path.split('/')[0].lower() == '.rcf-storage':
        raise ValueError('protected operation metadata')
    if entry.get('kind') != 'file':
        raise ValueError('only explicitly fingerprinted regular files may be deleted')
    sha = entry.get('sha256')
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
        raise ValueError('valid sha256 required')
    if entry.get('root_identity') != identity(os.fstat(scope.fd)):
        raise ValueError('root identity changed or evidence missing')
    if entry.get('root_ancestors') != [value for location, value in scope.chain]:
        raise ValueError('root ancestor identity changed or evidence missing')
    if not os.fstat(scope.fd).st_ino:
        raise ValueError('filesystem has no usable identity')
    parent, held, ancestors = scope.parent(path)
    try:
        if entry.get('ancestors') != ancestors:
            raise ValueError('ancestor identity changed or evidence missing')
        st = scope.lstat(path, parent)
        if link(st) or not stat.S_ISREG(st.st_mode) or st.st_dev != scope.device or not st.st_ino:
            raise ValueError('not an ordinary same-device file')
        for key, actual in [('bytes', st.st_size), ('mtime_ns', st.st_mtime_ns),
                            ('device', str(st.st_dev)), ('inode', str(st.st_ino)), ('nlink', st.st_nlink)]:
            if entry.get(key) != actual:
                raise ValueError('stat fingerprint changed: ' + key)
        fd = scope.open_file(path, parent, deleting=True)
        try:
            opened = os.fstat(fd)
            if signature(opened) != signature(st):
                raise ValueError('file replaced before verification')
            if digest(fd, opened, scope.deadline) != sha:
                raise ValueError('sha256 fingerprint changed')
            scope.verify_parent(path, ancestors)
            if signature(scope.lstat(path, parent)) != signature(opened):
                raise ValueError('file replaced before delete')
            if os.name == 'nt':
                # Delete by the verified handle, not by a path resolved again.
                disposition = wintypes.BOOL(True)
                handle = msvcrt.get_osfhandle(fd)
                if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition),
                                                          ctypes.sizeof(disposition)):
                    raise OSError(ctypes.get_last_error(), 'handle deletion failed')
            else:
                if os.unlink not in os.supports_dir_fd:
                    raise RuntimeError('safe descriptor unlink unavailable')
                os.unlink(path.split('/')[-1], dir_fd=parent)
            return st.st_size
        finally:
            os.close(fd)
    finally:
        for child in reversed(held):
            os.close(child)


def delete_entries(scope, entries, result, store=None):
    for entry in entries:
        path = entry.get('path') if isinstance(entry, dict) else None
        try:
            check_time(scope.deadline)
            if store is not None:
                store.verify()
            count = delete_one(scope, entry)
            result['deleted'].append(path)
            result['logical_deleted_bytes'] += count
        except FileNotFoundError:
            result['missing'].append(path)
        except (OSError, ValueError, RuntimeError, TypeError, AttributeError) as exc:
            result['blocked'].append(dict(path=path, reason=str(exc)))
    return result


def fenced_request(request, root, deadline):
    try:
        if protected_root(root):
            raise ValueError('home/system/filesystem root cleanup blocked')
        operation = request.get('operation') or {}
        operation_id = request.get('operation_id') if request['op'] == 'prepare_delete' else operation.get('operation_id')
        with Scope(request['root'], deadline) as scope:
            with OperationStore(scope, operation_id) as store:
                latest = store.latest
                if request['op'] == 'prepare_delete':
                    generation = latest['generation'] + 1 if latest else 1
                    previous = None
                    if latest:
                        if latest['state'] == 'complete':
                            previous = latest['complete_report']
                        elif latest['state'] == 'running':
                            previous = latest['interrupted_report']
                        else:
                            previous = latest.get('previous_report')
                    store.save(dict(generation=generation, state='prepared', previous_report=previous))
                    return dict(operation_id=operation_id, generation=generation,
                                state='prepared', previous_report=previous, errors=[])
                generation = operation.get('generation')
                if (type(generation) is not int or not latest or latest['generation'] != generation):
                    raise ValueError('fenced: stale or unprepared storage generation')
                if latest['state'] == 'complete':
                    return latest['complete_report']
                if latest['state'] != 'prepared':
                    raise ValueError('fenced: generation already started; prepare a new generation to reconcile')
                unknown = dict(deleted=[], missing=[], blocked=[], logical_deleted_bytes=0,
                               errors=[dict(path='', reason='interrupted helper; mutation outcome unknown')])
                # Persist the spent generation BEFORE any mutation. A crash can
                # lose a reply but can never authorize this generation twice.
                store.save(dict(generation=generation, state='running', interrupted_report=unknown))
                result = dict(deleted=[], missing=[], blocked=[], errors=[], logical_deleted_bytes=0)
                delete_entries(scope, request['entries'], result, store)
                store.save(dict(generation=generation, state='complete', complete_report=result))
                return result
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError) as exc:
        return dict(errors=[dict(path='', reason=str(exc))], fencing_error=str(exc))


def run(request):
    op = request['op']
    root = os.path.abspath(os.path.expanduser(request['root']))
    deadline = time.monotonic() + request.get('max_seconds', 30)
    if op == 'prepare_delete' or (op == 'delete' and request.get('operation') is not None):
        return fenced_request(request, root, deadline)
    if op == 'delete':
        result = dict(deleted=[], missing=[], blocked=[], errors=[], logical_deleted_bytes=0)
        entries = request['entries']
        try:
            if protected_root(root):
                raise ValueError('home/system/filesystem root cleanup blocked')
            with Scope(request['root'], deadline) as scope:
                delete_entries(scope, entries, result)
        except (OSError, ValueError, RuntimeError) as exc:
            for entry in entries:
                path = entry.get('path') if isinstance(entry, dict) else None
                result['blocked'].append(dict(path=path, reason=str(exc)))
        return result
    result = dict(root=root, complete=True, entries=[], errors=[], logical_bytes=0,
                  allocated_bytes=0, file_count=0,
                  observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    if op == 'inspect':
        result['missing'] = []
    try:
        with Scope(request['root'], deadline) as scope:
            result['root'] = scope.root
            if op == 'scan':
                scan(scope, result, request.get('max_entries', 20000),
                     request.get('exclude_names'))
            elif op == 'inspect':
                for path in request['paths']:
                    try:
                        check_time(deadline)
                        result['entries'].append(inspect_one(scope, path, request.get('hashes', True)))
                    except FileNotFoundError as exc:
                        result['missing'].append(path)
                        error(result, path, exc)
                    except (OSError, ValueError, RuntimeError) as exc:
                        error(result, path, exc)
                scope.verify()
            else:
                raise ValueError('unknown operation')
    except (OSError, ValueError, RuntimeError) as exc:
        error(result, '', exc)
    allocated_seen = set()
    for entry in result['entries']:
        if entry['kind'] == 'file':
            result['logical_bytes'] += entry['bytes']
            result['file_count'] += 1
            if entry['allocated_bytes'] is None:
                result['allocated_bytes'] = None
            elif result['allocated_bytes'] is not None:
                key = (entry['device'], entry['inode'])
                if entry['inode'] != '0' and key not in allocated_seen:
                    result['allocated_bytes'] += entry['allocated_bytes']
                    allocated_seen.add(key)
                elif entry['inode'] == '0':
                    result['allocated_bytes'] = None
    return result


if __name__ == '__main__':
    request = json.loads(base64.b64decode(REQUEST).decode('utf-8')) if REQUEST else json.load(sys.stdin)
    print(json.dumps(run(request), ensure_ascii=True, separators=(',', ':')))
'''

# Keep the source auditable while staying below Windows' 32767-character argv
# limit (the same SCRIPT is also invoked directly by boundary regression tests).
SCRIPT = "\n".join(line for line in SCRIPT.splitlines() if line.strip() and not line.lstrip().startswith("#"))
