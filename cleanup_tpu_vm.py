#!/usr/bin/python3
"""Preview or remove this user's TPU VM work files (Linux, Python 3.10+).

Examples:
  ./cleanup_tpu_vm.py                              # Preview all work files
  ./cleanup_tpu_vm.py --scope temp                 # Preview temp files/caches
  ./cleanup_tpu_vm.py --scope all --keep ~/CROSS    # Preserve a project
  ./cleanup_tpu_vm.py --scope all --apply --discard-local-work
  ./cleanup_tpu_vm.py --scope all-local             # Also preview credentials/config
  ./cleanup_tpu_vm.py --scope all-local --apply --discard-local-work

The last command permanently deletes the listed files, including unpublished
source, experiment evidence, checkpoints, environments, and shared-memory keys.
It makes no backup. Run only after saving wanted data and finishing workloads.

Scope 'temp' covers user-owned entries in /tmp, /var/tmp, /dev/shm, plus ~/.cache
and ~/.pytest_cache. Scope 'all' additionally covers home-directory contents,
preserving configured login/configuration and session paths and cleanup files.
Scope 'all-local' removes those exclusions: credentials, hidden configuration,
session data, old cleanup scripts and old reports become deletion candidates.
Deleting SSH keys and login configuration can prevent future access to the VM.
All scopes preserve OS files, other users' files, mounts, special files, paths
in use, explicit --keep paths, the running script's enclosing cleanup target,
and the current report. Symlinks are unlinked, never followed. No sudo, process
termination, service changes or remote writes. This is file deletion, not
secure disk erasure; user data outside the listed roots is not scanned.
Deletion is blocked while other user workloads are running. Process checks are
snapshots, not a scheduler lock: do not start jobs during cleanup.
"""

import argparse
import fnmatch
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import sys
import time


HOME_KEEP = {
    '.ssh', '.gnupg', '.config', '.codex', '.local', '.gitconfig', '.git-credentials',
    '.netrc', '.aws', '.azure', '.kube', '.docker', '.bashrc', '.bash_profile',
    '.bash_login', '.bash_logout', '.bash_history', '.profile', '.zshrc',
    '.zprofile', '.zsh_history', '.viminfo', '.vimrc', '.tmux.conf',
}
TEMP_KEEP = (
    'codex*', '.codex*', 'tmux-*', 'ssh-*', 'systemd-*', '.X*-unix',
    '.ICE-unix', '.X*-lock', 'vscode-*', '.vscode-*', 'libtpu*',
    'tpu_diag*', 'tpu_logs',
)
SESSION_NAMES = {
    'systemd', '(sd-pam)', 'dbus-daemon', 'sshd', 'tmux: server', 'tmux: client',
    'codex', 'codex-code-mode',
}
TEMP_ROOTS = (Path('/tmp'), Path('/var/tmp'), Path('/dev/shm'))


def beneath(path, parent):
    return path == parent or parent in path.parents


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid)


def mount_paths():
    """Include bind mounts, which need not change st_dev."""
    def unescape(value):
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
    return {Path(unescape(line.split()[4]))
            for line in Path('/proc/self/mountinfo').read_text().splitlines()}


def ancestors():
    result = set()
    pid = os.getpid()
    while pid and pid not in result:
        result.add(pid)
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        pid = int(fields[1])
    return result


def process_snapshot(uid):
    """Read process metadata only; never read environments or experiment data."""
    blockers, references, errors = [], [], []
    own_tree = ancestors()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        name = None
        try:
            if proc.stat().st_uid != uid:
                continue
            pid = int(proc.name)
            name = (proc / 'comm').read_text().strip()
            args = [os.fsdecode(x) for x in (proc / 'cmdline').read_bytes().split(b'\0') if x]
            fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                continue
            interactive_shell = name in {'bash', 'sh', 'zsh', 'fish'} and (
                len(args) <= 1 or all(a in {'-i', '-l', '-il', '-li'} for a in args[1:]))
            if pid not in own_tree and name not in SESSION_NAMES and not interactive_shell:
                blockers.append({'pid': pid, 'name': name})
            paths = []
            for link in [proc / 'cwd', proc / 'exe', *list((proc / 'fd').iterdir())]:
                try:
                    paths.append(os.readlink(link))
                except FileNotFoundError:
                    pass
            paths.extend(a.split('=', 1)[-1] for a in args)
            for value in paths:
                if value.startswith('/'):
                    value = value.removesuffix(' (deleted)')
                    references.append((Path(os.path.normpath(value)), pid))
        except FileNotFoundError:
            continue  # A process or descriptor exited during observation.
        except PermissionError as exc:
            # Linux intentionally hides the descriptors of privilege-separated
            # SSH/PAM session processes, which are allowed in every scope.
            if name not in {'sshd', '(sd-pam)'}:
                errors.append(f'Cannot inspect PID {proc.name}: {exc.strerror}')
        except OSError as exc:
            errors.append(f'Cannot inspect PID {proc.name}: {exc.strerror}')
    return blockers, references, errors


def tree_bytes(path, uid, mounts):
    """Metadata-only inventory; reject foreign owners, mounts and special files."""
    if any(beneath(mount, path) for mount in mounts):
        raise ValueError('contains a mount point')
    device = path.lstat().st_dev
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        info = current.lstat()
        if info.st_uid != uid:
            raise ValueError('contains files owned by another user')
        if info.st_dev != device:
            raise ValueError('crosses a filesystem boundary')
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)):
            raise ValueError('contains a socket, pipe or device')
        total += info.st_blocks * 512
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(current) as entries:
                pending.extend(Path(entry.path) for entry in entries)
    return total


def build_plan(home, scope, keep, references, uid, mounts, temp_roots=TEMP_ROOTS):
    candidates, skipped = [], []
    roots = list(temp_roots)
    if scope in {'all', 'all-local'}:
        roots.append(home)
    entries = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_symlink() or root.resolve() != root:
            raise ValueError(f'Refusing redirected cleanup root: {root}')
        entries.extend((entry, root == home) for entry in root.iterdir())
    if scope == 'temp':
        entries.extend((home / name, True) for name in ('.cache', '.pytest_cache')
                       if (home / name).exists() or (home / name).is_symlink())
    for path, in_home in sorted(entries):
        reason = None
        try:
            info = path.lstat()
            if info.st_uid != uid:
                reason = 'owned by another user'
            elif scope != 'all-local' and in_home and (path.name in HOME_KEEP or path.name.startswith(
                    ('cleanup_tpu_vm', 'test_cleanup_tpu_vm', 'cleanup-tpu-vm-report-'))):
                reason = 'configuration, credentials or cleanup tool'
            elif scope != 'all-local' and not in_home and any(
                    fnmatch.fnmatchcase(path.name, p) for p in TEMP_KEEP):
                reason = 'session or TPU runtime files'
            elif any(beneath(p, path) or beneath(path, p) for p in keep):
                reason = 'explicitly preserved'
            else:
                users = sorted({pid for ref, pid in references if beneath(ref, path)})
                if users:
                    reason = 'in use by PID(s): ' + ', '.join(map(str, users))
            if reason is None:
                size = tree_bytes(path, uid, mounts)
                candidates.append({'path': str(path), 'bytes': size,
                                   'identity': list(identity(info))})
                continue
        except (OSError, ValueError) as exc:
            reason = str(exc)
        skipped.append({'path': str(path), 'reason': reason})
    return candidates, skipped


def remove_target(target, uid, mounts):
    path = Path(target['path'])
    info = path.lstat()
    if list(identity(info)) != target['identity']:
        raise ValueError(f'Target changed since preview: {path}')
    tree_bytes(path, uid, mounts)
    if stat.S_ISDIR(info.st_mode):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError('This Python lacks symlink-safe directory removal')
        shutil.rmtree(path)
    else:
        path.unlink()


def deletion_gate(apply, discard, blockers, errors):
    if not apply:
        return None
    if not discard:
        return 'Deletion requires --discard-local-work (includes unpublished data).'
    if errors:
        return 'Deletion refused: process inspection was incomplete.'
    if blockers:
        return 'Deletion refused: workloads are still running. Finish them first.'
    return None


def human_size(size):
    return f'{size / (1024 ** 3):,.2f} GiB'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--scope', choices=['temp', 'all', 'all-local'], default='all',
                        help='all-local also selects credentials, configuration and session data')
    parser.add_argument('--apply', action='store_true', help='Delete the planned files')
    parser.add_argument('--discard-local-work', action='store_true',
                        help='Acknowledge permanent loss of selected data, including credentials in all-local scope')
    parser.add_argument('--keep', type=Path, action='append', default=[], metavar='PATH',
                        help='Preserve a path (repeatable; protects its enclosing cleanup target)')
    parser.add_argument('--report', type=Path, help='Write a new JSON report; never overwrite a file')
    parser.add_argument('--show-all', action='store_true', help='Print every proposed deletion')
    args = parser.parse_args(argv)
    uid = os.getuid()
    if uid == 0 or os.geteuid() != uid:
        parser.error('Run as the regular VM user, without sudo.')
    home = Path(pwd.getpwuid(uid).pw_dir)
    if home in {Path('/'), Path('/home')} or home.resolve() != home:
        parser.error('Refusing an unsafe or redirected home directory.')
    report = (args.report or home / (
        f'cleanup-tpu-vm-report-{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}-{os.getpid()}.json'
    )).expanduser().absolute()
    if report.exists() or report.is_symlink():
        parser.error(f'Report already exists; choose a new path: {report}')
    keep = [p.expanduser().absolute() for p in args.keep]
    keep.extend([Path(__file__).resolve(), report])
    blockers, references, errors = process_snapshot(uid)
    mounts = mount_paths()
    candidates, skipped = build_plan(home, args.scope, keep, references, uid, mounts)
    data = {'scope': args.scope, 'dry_run': not args.apply, 'uid': uid,
            'candidates': candidates, 'preserved': skipped, 'active_workloads': blockers,
            'inspection_errors': errors, 'deleted': [], 'deletion_errors': [], 'status': 'preview'}
    if args.scope == 'all-local':
        print('all-local includes credentials, hidden configuration and session data; '
              'deletion can prevent future login.')
    print(f'{len(candidates)} proposed deletions; approximately '
          f'{human_size(sum(t["bytes"] for t in candidates))} allocated (hard links may overcount).')
    print(f'{len(skipped)} entries preserved. Full inventory: {report}')
    for item in sorted(candidates, key=lambda x: (-x['bytes'], x['path']))[
            :None if args.show_all else 20]:
        print(f'  {human_size(item["bytes"]):>12}  {item["path"]}')
    if blockers:
        print('Active workloads (deletion blocked): ' + ', '.join(
            f'{p["pid"]} ({p["name"]})' for p in blockers))
    for error in errors:
        print(error, file=sys.stderr)
    gate = deletion_gate(args.apply, args.discard_local_work, blockers, errors)
    if gate:
        data['status'] = 'blocked'
        print(gate, file=sys.stderr)
    # Exclusive, private report: do not overwrite a user's file or follow a symlink.
    fd = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        def save():
            stream.seek(0)
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        save()
        if gate:
            return 2
        if not args.apply:
            print('Dry run complete. No files deleted.')
            return 0
        # Recheck after the potentially long filesystem inventory.
        blockers, live_refs, errors = process_snapshot(uid)
        gate = deletion_gate(True, True, blockers, errors)
        newly_in_use = [t['path'] for t in candidates if any(
            beneath(ref, Path(t['path'])) for ref, _ in live_refs)]
        if gate or newly_in_use:
            data.update(status='blocked', active_workloads=blockers, inspection_errors=errors,
                        newly_in_use=newly_in_use)
            save()
            print(gate or 'Deletion refused: planned paths are now in use.', file=sys.stderr)
            return 2
        data['status'] = 'applying'
        save()
        try:
            for target in candidates:
                remove_target(target, uid, mount_paths())
                data['deleted'].append(target['path'])
                print(f'Deleted: {target["path"]}', flush=True)
            data['status'] = 'complete'
        except (OSError, ValueError, KeyboardInterrupt) as exc:
            data['status'] = 'interrupted'
            data['deletion_errors'].append(str(exc) or 'Interrupted by user')
            print(f'Cleanup stopped: {exc}', file=sys.stderr)
        finally:
            save()
        print(f'Deleted {len(data["deleted"])} entries. Report: {report}')
        return 0 if data['status'] == 'complete' else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(f'Cleanup refused: {error}', file=sys.stderr)
        sys.exit(1)
