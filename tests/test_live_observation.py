"""Discovery failures remain visible while independent sources are inspected."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sumbi.live import snapshot
from sumbi.measure.live_sources import SourceProbe, live_inventory, open_rollouts


class ObservationTests(unittest.TestCase):
    def test_proc_failures_and_independent_fd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / '1'
            (base / 'task/1').mkdir(parents=True)
            children = base / 'task/1/children'
            children.write_text('')
            fd = base / 'fd'
            fd.mkdir()
            source = root / 'rollout-test.jsonl'
            source.write_text('{}\n')
            (fd / '1').symlink_to(source)
            (fd / '2').symlink_to(source)
            self.assertEqual(len(open_rollouts(1, root)[0]), 1)
            for method, error in [('read_text', 'proc_children_unreadable'),
                                  ('iterdir', 'proc_fd_unreadable'),
                                  ('readlink', 'proc_fd_entry_unreadable'),
                                  ('stat', 'proc_fd_entry_unreadable')]:
                if method == 'readlink':
                    import os
                    real = os.readlink
                    def fail(path, *args, **kwargs):
                        if str(path) == str(fd / '1'):
                            raise PermissionError()
                        return real(path, *args, **kwargs)
                    context = patch('os.readlink', side_effect=fail)
                else:
                    real = getattr(Path, method)
                    def fail(path, *args, **kwargs):
                        target = {'read_text': children, 'iterdir': fd, 'stat': fd / '1'}[method]
                        if path == target:
                            raise PermissionError()
                        return real(path, *args, **kwargs)
                    context = patch.object(Path, method, fail)
                with self.subTest(method=method), context:
                    paths, errors = open_rollouts(1, root)
                    self.assertIn(error, errors)
                    if method in ('readlink', 'stat', 'read_text'):
                        self.assertEqual(len(paths), 1)
                    inv = live_inventory(target='x', proc_root=root, sqlite_path=root / 'db',
                                         run=lambda _: '0\t\t\t\t\t1')
                    self.assertEqual(inv['windows'][0]['_error'], 'proc_observation_incomplete')
            children.unlink()
            self.assertIn('proc_children_unreadable', open_rollouts(1, root)[1])
            self.assertEqual(open_rollouts(99, root)[0], [])

    def test_unconfirmed_fragment_is_counted_and_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid, unknown = root / 'valid', root / 'unknown'
            valid.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'a'}}) + '\n')
            unknown.write_text('{}\n')
            window = SourceProbe().inventory({'windows': [{'engine': 'codex',
                'source_paths': [str(valid), str(unknown)]}]})['windows'][0]
            row = snapshot([window])['windows'][0]
            self.assertEqual(row['status'], 'unknown')
            self.assertEqual(row['excluded_source_count'], 1)
            self.assertEqual(row['excluded_source_reason'], 'source_format_unconfirmed_or_mismatch')
            self.assertEqual(row['reason'], 'source_fragments_unconfirmed')


if __name__ == '__main__':
    unittest.main()
