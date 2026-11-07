"""Cleanup safety checks. All deletions are confined to disposable fixtures."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cleanup_tpu_vm as cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix='cleanup-tpu-vm-test-')
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.home = self.root / 'home'
        self.tmp = self.root / 'tmp'
        self.home.mkdir()
        self.tmp.mkdir()
        self.uid = os.getuid()

    def file(self, name, content='keep or discard me'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def plan(self, scope='all', keep=(), references=(), mounts=()):
        return cleanup.build_plan(self.home, scope, list(keep), list(references),
                                  self.uid, set(mounts), [self.tmp])

    def test_default_inventory_does_not_delete(self):
        path = self.file('tmp/work/result.txt')
        candidates, _ = self.plan()
        self.assertEqual([t['path'] for t in candidates], [str(path.parent)])
        self.assertTrue(path.exists())

    def test_credentials_and_session_files_preserved(self):
        for name in ['home/.ssh/id_ed25519', 'home/.config/gh/hosts.yml',
                     'home/.codex/state', 'tmp/codex-daemon-2002/state',
                     'tmp/libtpu_lockfile', 'tmp/tmux-2002/socket-placeholder']:
            self.file(name)
        candidates, skipped = self.plan()
        self.assertEqual(candidates, [])
        self.assertEqual(len(skipped), 6)

    def test_temp_scope_preserves_repositories(self):
        self.file('home/CROSS/unpublished.py')
        self.file('home/.cache/pip/item')
        self.file('tmp/experiment/evidence.json')
        candidates, _ = self.plan(scope='temp')
        self.assertEqual({Path(t['path']).name for t in candidates}, {'.cache', 'experiment'})

    def test_all_local_selects_previously_excluded_files(self):
        for name in ['home/.ssh/key', 'home/.config/gh/hosts.yml', 'home/.codex/state',
                     'home/.local/data', 'home/.gitconfig', 'home/.bash_history',
                     'home/cleanup_tpu_vm_old.py', 'home/cleanup-tpu-vm-report-old.json',
                     'tmp/codex-old/state', 'tmp/tmux-old/regular-file', 'tmp/libtpu_lockfile']:
            self.file(name)
        candidates, skipped = self.plan(scope='all-local')
        self.assertEqual(len(candidates), 11)
        self.assertEqual(skipped, [])

    def test_all_local_still_preserves_kept_in_use_mounted_and_special_paths(self):
        kept = self.file('home/.ssh/key')
        active = self.file('home/.codex/state')
        mounted = self.file('tmp/work/mounted/data')
        os.mkfifo(self.tmp / 'pipe')
        candidates, skipped = self.plan(scope='all-local', keep=[kept],
                                        references=[(active, 123)], mounts=[mounted.parent])
        self.assertEqual(candidates, [])
        self.assertEqual(len(skipped), 4)

    def test_nested_keep_preserves_whole_project(self):
        wanted = self.file('home/CROSS/P5/unpublished.py')
        self.file('home/CROSS/other.txt')
        candidates, skipped = self.plan(keep=[wanted])
        self.assertEqual(candidates, [])
        self.assertEqual(skipped[0]['reason'], 'explicitly preserved')

    def test_active_reference_preserves_whole_tree(self):
        path = self.file('tmp/experiment/key')
        candidates, skipped = self.plan(references=[(path, 123)])
        self.assertEqual(candidates, [])
        self.assertIn('123', skipped[0]['reason'])

    def test_symlinks_are_unlinked_without_touching_target(self):
        outside = self.file('outside/valuable.txt')
        link = self.tmp / 'link'
        link.symlink_to(outside.parent, target_is_directory=True)
        candidates, _ = self.plan()
        cleanup.remove_target(candidates[0], self.uid, set())
        self.assertFalse(link.is_symlink())
        self.assertTrue(outside.exists())

    def test_nested_symlink_does_not_follow_target(self):
        outside = self.file('outside/valuable.txt')
        parent = self.tmp / 'work'
        parent.mkdir()
        (parent / 'link').symlink_to(outside.parent, target_is_directory=True)
        candidates, _ = self.plan()
        cleanup.remove_target(candidates[0], self.uid, set())
        self.assertFalse(parent.exists())
        self.assertTrue(outside.exists())

    def test_bind_mount_blocks_enclosing_directory(self):
        path = self.file('tmp/work/mounted/file')
        candidates, skipped = self.plan(mounts=[path.parent])
        self.assertEqual(candidates, [])
        self.assertIn('mount point', skipped[0]['reason'])

    def test_special_files_are_preserved(self):
        pipe = self.tmp / 'pipe'
        os.mkfifo(pipe)
        candidates, skipped = self.plan()
        self.assertEqual(candidates, [])
        self.assertIn('pipe', skipped[0]['reason'])

    def test_foreign_ownership_is_rejected(self):
        path = self.file('tmp/data')
        with self.assertRaisesRegex(ValueError, 'another user'):
            cleanup.tree_bytes(path, self.uid + 1, set())

    def test_replaced_target_is_not_removed(self):
        path = self.file('tmp/data')
        candidates, _ = self.plan()
        path.rename(self.tmp / 'original')
        self.file('tmp/data', 'new contents')
        with self.assertRaisesRegex(ValueError, 'changed since preview'):
            cleanup.remove_target(candidates[0], self.uid, set())
        self.assertEqual(path.read_text(), 'new contents')

    def test_apply_gates(self):
        self.assertIsNone(cleanup.deletion_gate(False, False, [123], ['error']))
        self.assertIn('discard', cleanup.deletion_gate(True, False, [], []))
        self.assertIn('workloads', cleanup.deletion_gate(True, True, [123], []))
        self.assertIn('incomplete', cleanup.deletion_gate(True, True, [], ['error']))
        self.assertIsNone(cleanup.deletion_gate(True, True, [], []))

    def simulated_process(self, name):
        proc = self.root / '4300'
        proc.mkdir(exist_ok=True)
        (proc / 'comm').write_text(name)
        (proc / 'cmdline').write_bytes(name.encode() + b'\0')
        (proc / 'stat').write_text(f'4300 ({name}) S 1 0 0')
        (proc / 'fd').mkdir(exist_ok=True)
        original_iterdir = Path.iterdir
        def iterdir(path):
            return iter([proc]) if str(path) == '/proc' else original_iterdir(path)
        with patch.object(Path, 'iterdir', iterdir), \
                patch.object(cleanup, 'ancestors', return_value=set()), \
                patch.object(cleanup.os, 'readlink', side_effect=PermissionError(13, 'Permission denied')):
            return cleanup.process_snapshot(self.uid)

    def test_hidden_ssh_metadata_does_not_block_cleanup(self):
        blockers, _, errors = self.simulated_process('sshd')
        self.assertEqual(blockers, [])
        self.assertEqual(errors, [])

    def test_hidden_workload_metadata_still_blocks_cleanup(self):
        blockers, _, errors = self.simulated_process('python')
        self.assertEqual(len(blockers), 1)
        self.assertEqual(len(errors), 1)

    def run_main(self, args, snapshots):
        original_plan = cleanup.build_plan
        def fixture_plan(home, scope, keep, references, uid, mounts):
            return original_plan(home, scope, keep, references, uid, mounts, [self.tmp])
        with patch.object(cleanup.pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir=str(self.home))), \
                patch.object(cleanup, 'process_snapshot', side_effect=snapshots), \
                patch.object(cleanup, 'mount_paths', return_value=set()), \
                patch.object(cleanup, 'build_plan', side_effect=fixture_plan), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cleanup.main(args)

    def test_main_dry_run_writes_report_and_preserves_files(self):
        data = self.file('tmp/data')
        report = self.home / 'preview.json'
        self.assertEqual(self.run_main(['--report', str(report)], [([], [], [])]), 0)
        self.assertTrue(data.exists())
        self.assertEqual(json.loads(report.read_text())['status'], 'preview')
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_main_active_worker_blocks_all_deletions(self):
        data = self.file('tmp/data')
        report = self.home / 'blocked.json'
        result = self.run_main(['--apply', '--discard-local-work', '--report', str(report)],
                               [([{'pid': 123, 'name': 'python'}], [], [])])
        self.assertEqual(result, 2)
        self.assertTrue(data.exists())
        self.assertEqual(json.loads(report.read_text())['status'], 'blocked')

    def test_new_worker_after_inventory_blocks_deletion(self):
        data = self.file('tmp/data')
        result = self.run_main(['--apply', '--discard-local-work'],
                               [([], [], []), ([{'pid': 124, 'name': 'python'}], [], [])])
        self.assertEqual(result, 2)
        self.assertTrue(data.exists())

    def test_main_apply_deletes_only_fixture_targets(self):
        data = self.file('tmp/discard/data')
        credentials = self.file('home/.ssh/key')
        wanted = self.file('home/wanted/data')
        result = self.run_main(['--apply', '--discard-local-work', '--keep', str(wanted)],
                               [([], [], []), ([], [], [])])
        self.assertEqual(result, 0)
        self.assertFalse(data.exists())
        self.assertTrue(credentials.exists())
        self.assertTrue(wanted.exists())

    def test_existing_report_is_not_overwritten(self):
        report = self.file('home/report.json', 'valuable contents')
        with self.assertRaises(SystemExit):
            self.run_main(['--report', str(report)], [])
        self.assertEqual(report.read_text(), 'valuable contents')

    def test_all_local_dry_run_includes_credentials_without_deleting(self):
        credentials = self.file('home/.ssh/key')
        report = self.home / 'preview.json'
        result = self.run_main(['--scope', 'all-local', '--report', str(report)], [([], [], [])])
        self.assertEqual(result, 0)
        self.assertTrue(credentials.exists())
        data = json.loads(report.read_text())
        self.assertEqual(data['scope'], 'all-local')
        self.assertEqual(data['candidates'][0]['path'], str(credentials.parent))
        self.assertEqual(data['deleted'], [])

    def test_all_local_apply_removes_local_data_but_preserves_runner_report_and_keep(self):
        credentials = self.file('home/.ssh/key')
        config = self.file('home/.config/gh/hosts.yml')
        project = self.file('home/project/unpublished.py')
        old_report = self.file('home/cleanup-tpu-vm-report-old.json')
        temp = self.file('tmp/codex-old/data')
        runner = self.file('home/tool/cleanup_tpu_vm.py')
        wanted = self.file('home/wanted/data')
        report = self.home / 'current-report.json'
        with patch.object(cleanup, '__file__', str(runner)):
            result = self.run_main(['--scope', 'all-local', '--apply', '--discard-local-work',
                                    '--keep', str(wanted), '--report', str(report)],
                                   [([], [], []), ([], [], [])])
        self.assertEqual(result, 0)
        for path in [credentials, config, project, old_report, temp]:
            self.assertFalse(path.exists(), str(path))
        for path in [runner, wanted, report]:
            self.assertTrue(path.exists(), str(path))
        data = json.loads(report.read_text())
        self.assertEqual(data['status'], 'complete')
        self.assertEqual(len(data['deleted']), 5)

    def test_all_local_apply_requires_discard_acknowledgement(self):
        credentials = self.file('home/.ssh/key')
        result = self.run_main(['--scope', 'all-local', '--apply'], [([], [], [])])
        self.assertEqual(result, 2)
        self.assertTrue(credentials.exists())

    def test_all_local_active_worker_still_blocks_deletion(self):
        credentials = self.file('home/.ssh/key')
        result = self.run_main(['--scope', 'all-local', '--apply', '--discard-local-work'],
                               [([{'pid': 123, 'name': 'python'}], [], [])])
        self.assertEqual(result, 2)
        self.assertTrue(credentials.exists())


if __name__ == '__main__':
    unittest.main()
