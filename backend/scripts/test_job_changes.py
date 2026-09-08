"""Synthetic parquet integration fixtures; no network or production uploads."""
import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import duckdb

spec = importlib.util.spec_from_file_location('changes', Path(__file__).with_name('build-job-changes.py'))
changes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changes)
SCHEMA = dict(ats='VARCHAR', slug='VARCHAR', id='VARCHAR', title='VARCHAR', location='VARCHAR',
              url='VARCHAR', content='VARCHAR', embed_status='VARCHAR', published_at='TIMESTAMPTZ',
              first_seen_at='TIMESTAMPTZ', is_open='BOOLEAN', op='VARCHAR', from_date='VARCHAR',
              to_date='VARCHAR', removal='VARCHAR', removed_at_crawler='TIMESTAMPTZ')
NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


class ChangesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'feed'
        self.anchor = {'diff': '2026-09-07__2026-09-08', 'content_sha256': 'a' * 64}

    def job(self, identity='1', **extra):
        return dict(dict(ats='example', slug='company', id=identity, title='Engineer',
                    location='Phoenix, AZ', url='https://example.test/job/' + identity,
                    content='Build things', embed_status='done', published_at=NOW,
                    first_seen_at=NOW, is_open=True, removal=None, removed_at_crawler=None), **extra)

    def parquet(self, path, rows, schema=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        schema = schema or SCHEMA
        with duckdb.connect() as con:
            con.execute('CREATE TABLE jobs (' + ','.join(f'"{k}" {v}' for k, v in schema.items()) + ')')
            if rows:
                con.executemany('INSERT INTO jobs VALUES (' + ','.join('?' for _ in schema) + ')',
                                [[row.get(k) for k in schema] for row in rows])
            con.execute('COPY jobs TO ? (FORMAT PARQUET)', [str(path)])

    def bootstrap(self, rows=None, day='2026-09-08', previous=None):
        rows = rows if rows is not None else [self.job()]
        folder = self.root / day
        (folder / 'web').mkdir(parents=True, exist_ok=True)
        self.parquet(folder / 'jobs/example.parquet', rows)
        (folder / 'web/manifest.json').write_bytes(changes.encode({'built_at': 123}))
        index = self.root / (day + '-index.json')
        index.write_bytes(changes.encode({'head': day, 'snapshot_built_at': 123, 'entries': [
            {'from': self.anchor['diff'].split('__')[0], 'to': day,
             'content_sha256': self.anchor['content_sha256'], 'new_jobs_after_carry': len(rows)}]}))
        return changes.build(self.out, snapshot=folder, index=index, previous=previous)

    def diff(self, events, previous, day='2026-09-09'):
        folder = self.root / 'diffs' / (previous['cursor'] + '__' + day)
        rows = [dict(row, op=op, from_date=previous['cursor'], to_date=day) for op, row in events]
        part = folder / 'lite/data_0.parquet'
        self.parquet(part, rows)
        full_hash = 'b' * 64
        side = {'schema_version': 2, 'carry_done': True, 'from': previous['cursor'], 'to': day,
                'parent': previous['source'], 'change_key': sorted(changes.CHANGE_KEY),
                'counts': {op: sum(o == op for o, _ in events) for op in ('added', 'changed', 'removed', 'carried')},
                'parts': [{'file': 'data_0.parquet', 'sha256': full_hash}],
                'content_sha256': changes.digest(f'data_0.parquet {full_hash}'.encode()),
                'lite': {'parts': [{'file': part.name, 'bytes': part.stat().st_size,
                                    'sha256': changes.sha256_file(part)}]}}
        path = folder.with_suffix('.json'); path.write_bytes(changes.encode(side))
        return path

    def folder(self, header):
        return self.out / 'changes' / header['generation']

    def rows(self, header):
        return list(changes.verified_rows(self.folder(header), header))

    def test_bootstrap_delta_replay_and_recovery(self):
        initial = self.bootstrap([self.job(), self.job('2')])
        side = self.diff([('changed', self.job(location='San Diego, CA')), ('changed_prev', self.job()),
                          ('added', self.job('3')), ('removed', self.job('2', removal='closed'))], initial)
        delta = changes.build(self.out, diff=side, previous=initial)
        self.assertEqual(delta['counts'], {'upsert': 2, 'remove': 1})
        self.assertEqual(changes.build(self.out, diff=side, previous=initial), delta)
        with sqlite3.connect(':memory:') as db:
            for header in (initial, delta, delta):
                changes.apply_generation(db, self.folder(header), header)
            self.assertEqual(db.execute('SELECT key FROM jobs ORDER BY key').fetchall(),
                             [('example/company#1',), ('example/company#3',)])
            self.anchor = delta['source']
            fresh = self.bootstrap([self.job('4')], day='2026-09-09', previous=delta)
            with self.assertRaisesRegex(ValueError, 'explicit reset'):
                changes.apply_generation(db, self.folder(fresh), fresh)
            changes.apply_generation(db, self.folder(fresh), fresh, reset=True)
            self.assertEqual(db.execute('SELECT key FROM jobs').fetchall(), [('example/company#4',)])

    def test_carried_and_changed_prev_emit_no_event(self):
        initial = self.bootstrap()
        side = self.diff([('carried', self.job()), ('changed_prev', self.job('2')),
                          ('changed', self.job('2', embed_status='pending'))], initial)
        delta = changes.build(self.out, diff=side, previous=initial)
        self.assertEqual([r['key'] for r in self.rows(delta)], ['example/company#2'])
        self.assertEqual(self.rows(delta)[0]['job']['embed_status'], 'pending')

    def test_removal_reasons_and_embedding_transitions(self):
        initial = self.bootstrap()
        for reason in ('closed', 'left_dataset', 'unknown'):
            with self.subTest(reason=reason):
                side = self.diff([('removed', self.job(removal=reason, removed_at_crawler=NOW))], initial)
                delta = changes.build(self.out, diff=side, previous=initial)
                self.assertEqual(self.rows(delta)[0]['removal'], reason)
        for status in ('pending', 'done', 'error'):
            side = self.diff([('changed_prev', self.job()), ('changed', self.job(embed_status=status))], initial)
            delta = changes.build(self.out, diff=side, previous=initial)
            self.assertEqual(self.rows(delta)[0]['job']['embed_status'], status)

    def test_projected_mutable_fields_and_explicit_exclusions(self):
        initial = self.bootstrap()
        updates = dict(title='Manager', location='Mesa, CO', url='https://example.test/new',
                       content='New description', embed_status='pending',
                       published_at=datetime(2026, 9, 9, tzinfo=timezone.utc))
        side = self.diff([('changed_prev', self.job()), ('changed', self.job(**updates))], initial)
        job = self.rows(changes.build(self.out, diff=side, previous=initial))[0]['job']
        self.assertEqual(set(job), set(changes.FIELDS))
        for field, value in updates.items():
            self.assertEqual(job[field], changes.timestamp(value) if field == 'published_at' else value)
        self.assertNotIn('company', job)
        self.assertNotIn('last_seen_at', job)

    def test_empty_delta_advances_checkpoint(self):
        initial = self.bootstrap()
        delta = changes.build(self.out, diff=self.diff([], initial), previous=initial)
        self.assertEqual(delta['pages'], [])
        with sqlite3.connect(':memory:') as db:
            for header in (initial, delta): changes.apply_generation(db, self.folder(header), header)
            self.assertEqual(db.execute('SELECT generation FROM checkpoint').fetchone()[0], delta['generation'])

    def test_source_gap_parent_hash_and_incomplete_diff_fail(self):
        initial = self.bootstrap()
        for field, value, message in [('from', '2026-09-07', 'gap'), ('parent', None, 'gap'),
                                      ('carry_done', False, 'completed'), ('change_key', [], 'track'),
                                      ('content_sha256', '0' * 64, 'digest')]:
            with self.subTest(field=field):
                path = self.diff([], initial)
                side = changes.read_json(path); side[field] = value
                path.write_bytes(changes.encode(side))
                with self.assertRaisesRegex(ValueError, message):
                    changes.build(self.out, diff=path, previous=initial)

    def test_duplicate_conflicting_unpaired_and_incomplete_rows_fail(self):
        initial = self.bootstrap()
        for rows in [[('added', self.job()), ('added', self.job())],
                     [('changed', self.job()), ('changed_prev', self.job('2'))],
                     [('carried', self.job()), ('added', self.job())], [('changed', self.job())]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                changes.build(self.out, diff=self.diff(rows, initial), previous=initial)
        side = self.diff([], initial)
        data = changes.read_json(side); data['counts']['added'] = 1
        side.write_bytes(changes.encode(data))
        with self.assertRaisesRegex(ValueError, 'counts'):
            changes.build(self.out, diff=side, previous=initial)

    def test_corrupt_missing_and_unsafe_source_parts_fail(self):
        initial = self.bootstrap()
        path = self.diff([], initial)
        part = path.with_suffix('') / 'lite/data_0.parquet'; part.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            changes.build(self.out, diff=path, previous=initial)
        part.unlink()
        with self.assertRaises(FileNotFoundError): changes.build(self.out, diff=path, previous=initial)
        side = changes.read_json(path); side['lite']['parts'][0]['file'] = '../elsewhere.parquet'
        path.write_bytes(changes.encode(side))
        with self.assertRaisesRegex(ValueError, 'part name'): changes.build(self.out, diff=path, previous=initial)

    def test_bootstrap_alignment_cutoff_counts_and_duplicates(self):
        self.bootstrap()
        folder = self.root / '2026-09-08'; path = self.root / '2026-09-08-index.json'
        original = changes.read_json(path)
        for field, value in [('head', '2026-09-09'), ('snapshot_built_at', 124)]:
            path.write_bytes(changes.encode(dict(original, **{field: value})))
            with self.assertRaises(ValueError): changes.build(self.out, snapshot=folder, index=path)
        data = copy.deepcopy(original); data['entries'][0]['new_jobs_after_carry'] = 5
        path.write_bytes(changes.encode(data))
        with self.assertRaisesRegex(ValueError, 'count'): changes.build(self.out, snapshot=folder, index=path)
        with self.assertRaisesRegex(ValueError, 'September 8'): self.bootstrap(day='2026-09-07')
        with self.assertRaisesRegex(ValueError, 'duplicate'): self.bootstrap([self.job(), self.job()])

    def test_page_bounds_and_oversize(self):
        header = self.bootstrap([self.job(str(i)) for i in range(1001)])
        self.assertEqual([p['rows'] for p in header['pages']], [1000, 1])
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            self.bootstrap([self.job(content='x' * changes.MAX_BYTES)])

    def test_each_upload_failure_preserves_head_and_retry_succeeds(self):
        header = self.bootstrap()
        for fail_at in range(len(header['pages']) + 2):
            remote, writes = [None], []
            def upload(key, path):
                if len(writes) == fail_at: raise RuntimeError('interrupted')
                writes.append(key)
                if key == 'changes/latest.json': remote[0] = changes.read_json(path)
            with self.assertRaises(RuntimeError):
                changes.publish(self.out, header, '', lambda _: remote[0], upload)
            self.assertIsNone(remote[0])
            def success(key, path):
                if key == 'changes/latest.json': remote[0] = changes.read_json(path)
            for _ in range(2): changes.publish(self.out, header, '', lambda _: remote[0], success)
            self.assertEqual(remote[0], header)

    def test_stale_parent_competing_publisher_and_manifest_corruption(self):
        header = self.bootstrap()
        other = dict(header, cursor='2026-09-10'); other.pop('generation')
        other['generation'] = changes.digest(changes.encode(other))
        with self.assertRaisesRegex(ValueError, 'remote head differs'):
            changes.publish(self.out, header, '', lambda _: other, lambda *a: self.fail('upload'))
        heads = iter([None, other]); writes = []
        with self.assertRaisesRegex(ValueError, 'changed during upload'):
            changes.publish(self.out, header, '', lambda _: next(heads), lambda key, path: writes.append(key))
        self.assertNotIn('changes/latest.json', writes)
        (self.folder(header) / 'manifest.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            changes.publish(self.out, header, '', lambda _: None, lambda *a: self.fail('upload'))

    def test_consumer_gap_and_corruption_roll_back_data_and_cursor(self):
        initial = self.bootstrap()
        with patch.object(changes, 'MAX_ROWS', 1):
            delta = changes.build(self.out, diff=self.diff([('added', self.job('2')),
                                    ('added', self.job('3'))], initial), previous=initial)
        with sqlite3.connect(':memory:') as db:
            with self.assertRaisesRegex(ValueError, 'gap'): changes.apply_generation(db, self.folder(delta), delta)
            changes.apply_generation(db, self.folder(initial), initial)
            (self.folder(delta) / delta['pages'][1]['file']).write_bytes(b'bad')
            with self.assertRaisesRegex(ValueError, 'checksum'): changes.apply_generation(db, self.folder(delta), delta)
            self.assertEqual(db.execute('SELECT key FROM jobs').fetchall(), [('example/company#1',)])
            self.assertEqual(db.execute('SELECT generation FROM checkpoint').fetchone()[0], initial['generation'])

    def test_final_readback_must_match(self):
        header = self.bootstrap()
        with self.assertRaisesRegex(ValueError, 'readback mismatch'):
            changes.publish(self.out, header, '', lambda _: None, lambda *a: None)

    def test_candidate_cli_needs_no_old_export_and_success_receipt_is_separate(self):
        header = self.bootstrap()
        candidate = self.folder(header) / 'manifest.json'
        with patch.object(sys, 'argv', ['build-job-changes.py', '--out', str(self.out),
                         '--candidate', str(candidate), '--publish-base', 'https://example.test']), \
                patch.object(changes, 'build', side_effect=AssertionError('must not read exports')), \
                patch.object(changes, 'publish', side_effect=RuntimeError('network failure')):
            with self.assertRaises(RuntimeError): changes.main()
        self.assertFalse((self.out / 'published.json').exists())
        with patch.object(sys, 'argv', ['build-job-changes.py', '--out', str(self.out),
                         '--candidate', str(candidate), '--publish-base', 'https://example.test']), \
                patch.object(changes, 'publish'):
            changes.main()
        self.assertEqual(changes.read_json(self.out / 'published.json'), header)

    def test_real_upstream_diff_visible_changes_removal_and_carry(self):
        old = self.root / 'export/2026-09-08'; new = self.root / 'export/2026-09-09'
        schema = {k: v for k, v in SCHEMA.items() if k not in
                  ('op', 'from_date', 'to_date', 'removal', 'removed_at_crawler')}
        schema.update({k: 'VARCHAR' for k in ('raw_json', 'detail_raw_json', 'enrichment_json', 'embedding')})
        before = [self.job(str(i)) for i in range(8)] + [self.job('carry', slug='absent')]
        before[6]['content'] = None
        after = copy.deepcopy(before[:7])
        after[6]['content'] = ''  # equivalent under upstream coalesce(content, '')
        values = ['New title', 'Grand Junction, CO', 'https://example.test/moved',
                  'New description', 'pending', datetime(2026, 9, 9, tzinfo=timezone.utc)]
        for row, field, value in zip(after, ['title', 'location', 'url', 'content', 'embed_status', 'published_at'], values):
            row[field] = value
        self.parquet(old / 'jobs/example.parquet', before, schema)
        self.parquet(new / 'jobs/example.parquet', after, schema)
        ledger_schema = dict(ats='VARCHAR', slug='VARCHAR', id='VARCHAR', is_open='BOOLEAN', removed_at='TIMESTAMPTZ')
        self.parquet(self.root / 'export/ledger/2026-09-09/data_0.parquet',
                     [dict(ats='example', slug='company', id='7', is_open=False, removed_at=NOW)], ledger_schema)
        diffs = self.root / 'export/diffs'; diffs.mkdir(parents=True)
        (diffs / '2026-09-07__2026-09-08.json').write_bytes(changes.encode(
            {'content_sha256': self.anchor['content_sha256']}))
        proc = subprocess.run([sys.executable, str(Path(__file__).with_name('build-diff.py')),
                               '--prev', str(old), '--new', str(new), '--out', str(self.root / 'export/diffs'),
                               '--no-verify'], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        side = changes.read_json(self.root / 'export/diffs/2026-09-08__2026-09-09.json')
        self.assertEqual(side['counts'], {'added': 0, 'removed': 1, 'changed': 6, 'carried': 1})
        self.assertEqual(side['removal'], {'closed': 1})
        initial = self.bootstrap(before)
        delta = changes.build(self.out, diff=diffs / '2026-09-08__2026-09-09.json', previous=initial)
        self.assertEqual(delta['counts'], {'upsert': 6, 'remove': 1})
        with sqlite3.connect(':memory:') as db:
            for header in (initial, delta): changes.apply_generation(db, self.folder(header), header)
            keys = {r[0] for r in db.execute('SELECT key FROM jobs')}
            self.assertIn('example/absent#carry', keys)
            self.assertNotIn('example/company#7', keys)
            self.assertEqual(len(keys), 8)


if __name__ == '__main__':
    unittest.main()
