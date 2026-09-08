import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('changes', Path(__file__).with_name('build-job-changes.py'))
changes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changes)


class ChangesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def snapshot(self, name, jobs, timestamp, leaf=0):
        web = self.root / name
        (web / 'groups').mkdir(parents=True)
        (web / 'manifest.json').write_text(json.dumps({'built_at': timestamp, 'jobs': len(jobs),
            'leaves': 1, 'tree': [{'id': leaf, 'size': len(jobs), 'children': []}]}))
        (web / 'groups' / f'{leaf}.json').write_text(json.dumps({'leaf': leaf, 'jobs': jobs}))
        return web

    def job(self, identity='1', **extra):
        return dict(ats='example', slug='company', id=identity, title='Engineer',
                    location='Phoenix, AZ', jd='Build things', seen=100, pub=100, **extra)

    def rows(self, web, header):
        return [json.loads(line) for page in header['pages']
                for line in (web / 'changes' / header['generation'] / page['file']).read_text().splitlines()]

    def test_bootstrap_delta_and_replay(self):
        first = self.snapshot('first', [self.job(), self.job('2')], 1)
        initial = changes.build(first)
        updated = self.job(); updated['location'] = 'San Diego, CA'
        second = self.snapshot('second', [updated, self.job('3')], 2, leaf=17)
        delta = changes.build(second, first)
        self.assertEqual(delta['previous'], initial['generation'])
        self.assertEqual(delta['counts'], {'upsert': 2, 'remove': 1})
        state = {}
        for row in self.rows(first, initial) + self.rows(second, delta) * 2:
            if row['op'] == 'remove': state.pop(row['key'], None)
            else: state[row['key']] = row['job']
        self.assertEqual(set(state), {'example/company#1', 'example/company#3'})
        self.assertEqual(state['example/company#1']['location'], 'San Diego, CA')
        self.assertEqual(changes.build(second, first), delta)

    def test_reclustering_vectors_and_unknown_fields_do_not_emit_changes(self):
        first = self.snapshot('first', [self.job(v='old', last_seen_ms=1)], 1)
        changes.build(first)
        second = self.snapshot('second', [self.job(v='new', last_seen_ms=2)], 2, leaf=99)
        delta = changes.build(second, first)
        self.assertEqual(delta['pages'], [])
        self.assertEqual(delta['counts'], {'upsert': 0, 'remove': 0})

    def test_incomplete_or_duplicate_input_fails(self):
        web = self.snapshot('missing', [self.job()], 1)
        (web / 'groups/0.json').unlink()
        with self.assertRaises(FileNotFoundError): changes.build(web)
        self.assertFalse((web / 'changes/latest.json').exists())
        web = self.snapshot('duplicate', [self.job(), self.job()], 1)
        with self.assertRaises(changes.sqlite3.IntegrityError): changes.build(web)

    def test_previous_content_must_match_published_digest(self):
        first = self.snapshot('first', [self.job()], 1)
        changes.build(first)
        (first / 'groups/0.json').write_text(json.dumps({'leaf': 0, 'jobs': [self.job('changed')]}))
        second = self.snapshot('second', [self.job()], 2)
        with self.assertRaisesRegex(ValueError, 'content changed'): changes.build(second, first)

    def test_page_bounds_and_oversized_record(self):
        web = self.snapshot('many', [self.job(str(i)) for i in range(1001)], 1)
        header = changes.build(web)
        self.assertEqual([p['rows'] for p in header['pages']], [1000, 1])
        job = self.job(); job['jd'] = 'x' * changes.MAX_BYTES
        web = self.snapshot('large', [job], 2)
        with self.assertRaisesRegex(ValueError, 'byte limit'): changes.build(web)

    def test_publication_failure_never_advances_head_and_retry_succeeds(self):
        web = self.snapshot('first', [self.job()], 1)
        header = changes.build(web)
        writes, remote = [], [None]
        def fail(key, path):
            writes.append(key)
            raise RuntimeError('upload failed')
        with self.assertRaises(RuntimeError):
            changes.publish(web, header, 'https://example.test', lambda _: remote[0], fail)
        self.assertNotIn('changes/latest.json', writes)
        def upload(key, path):
            writes.append(key)
            if key == 'changes/latest.json': remote[0] = changes.read_json(path)
        changes.publish(web, header, 'https://example.test', lambda _: remote[0], upload)
        self.assertEqual(writes[-1], 'changes/latest.json')
        self.assertEqual(remote[0], header)
        changes.publish(web, header, 'https://example.test', lambda _: remote[0], upload)

    def test_stale_parent_and_page_corruption_fail_closed(self):
        web = self.snapshot('first', [self.job()], 1)
        header = changes.build(web)
        def forbidden(*args): self.fail('must not upload')
        with self.assertRaisesRegex(ValueError, 'remote head differs'):
            changes.publish(web, header, '', lambda _: {'generation': 'another'}, forbidden)
        (web / 'changes' / header['generation'] / header['pages'][0]['file']).write_text('corrupt')
        with self.assertRaisesRegex(ValueError, 'page changed'):
            changes.publish(web, header, '', lambda _: None, forbidden)


if __name__ == '__main__':
    unittest.main()
