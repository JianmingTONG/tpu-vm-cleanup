# TPU VM cleanup

A standalone Python script to preview and remove user-owned work files on a
Linux TPU VM. It uses the Python standard library and defaults to a dry run.

Requires Linux with `/proc`, Python 3.10 or newer, and a regular user account.
Run without `sudo`.

## Usage

```bash
git clone https://github.com/JianmingTONG/tpu-vm-cleanup.git
cd tpu-vm-cleanup

# Preview cleanup of home-directory work files, temporary files, and caches.
/usr/bin/python3 cleanup_tpu_vm.py

# Preview only temporary files and caches.
/usr/bin/python3 cleanup_tpu_vm.py --scope temp

# Preserve a particular project. Repeat --keep for additional paths.
/usr/bin/python3 cleanup_tpu_vm.py --keep ~/my-project
```

Each run writes a new JSON inventory with owner-only permissions and prints its
location. Use `--show-all` to print every proposed deletion, or `--report PATH`
to choose a new report file. Existing report files are never overwritten.

After reviewing the inventory, saving wanted data, and finishing workloads:

```bash
/usr/bin/python3 cleanup_tpu_vm.py --scope all --apply --discard-local-work
```

**Deletion is permanent.** This command can remove local repositories (including
uncommitted and unpushed work), datasets, checkpoints, environments, experiment
evidence, and shared-memory keys. It creates no backup. Include the same `--keep`
options when applying a preview. Applying performs a fresh inventory; it does
not replay a saved report.

## Scope and protections

| Scope | Files considered |
| --- | --- |
| `temp` | User-owned entries in `/tmp`, `/var/tmp`, and `/dev/shm`, plus `~/.cache` and `~/.pytest_cache` |
| `all` (default) | Everything in `temp`, plus other entries in the user's home directory |

The script preserves configured credential and shell-configuration paths,
Codex/session directories, TPU runtime files, its own enclosing directory, and
cleanup reports. The exact exclusions are `HOME_KEEP` and `TEMP_KEEP` in the
script. Credentials stored elsewhere are not automatically recognized; use
`--keep` for any additional paths you need to preserve.

It also preserves paths referenced by observed user processes, mount points
and their enclosing cleanup targets, other users' files, and directories
containing sockets, pipes, or devices. Symlinks are unlinked without deleting
their targets. A nested `--keep` path preserves its whole top-level cleanup
target.

Deletion is blocked while other user workloads are running or workload process
inspection is incomplete. The script never stops processes. It rechecks
processes after inventory and checks target identity before removal. These
checks are snapshots, not a scheduler lock: run during a maintenance window
and do not start jobs during cleanup.

## Tests

```bash
/usr/bin/python3 -B -m unittest -v test_cleanup_tpu_vm
```

The 19 tests cover previews, deletion gates, workload detection, credential
exclusions, explicit preservation, symlinks, mounts, special files, ownership,
target replacement, and report handling. Test deletions use disposable fixtures.

Exit status is `0` for a successful preview or cleanup, `2` for a blocked apply
or invalid arguments, and `1` for an operational error or interrupted cleanup.
