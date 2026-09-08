#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.1"]
# ///
"""Project verified crawler diffs into an immutable, paged feed; see JOB-CHANGES.md."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid

VERSION = 2
SCOPE = 'crawler-export-v1'
FIRST_DATE = '2026-09-08'
FIELDS = ('ats', 'slug', 'id', 'title', 'location', 'url', 'content',
          'embed_status', 'published_at', 'first_seen_at')
CHANGE_KEY = {'title', 'location', 'url', 'content', 'embed_status', 'published_at'}
MAX_ROWS = 1000
MAX_BYTES = 4 * 1024 * 1024


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def require_hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('invalid sha256')
    return value


def require_date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('invalid export date')
    return value


def part_paths(folder, parts, suffix):
    names = set()
    for part in parts:
        name = part['file']
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.' + suffix, name)
                or name in names):
            raise ValueError('invalid or duplicate part name')
        names.add(name)
        require_hash(part['sha256'])
        path = Path(folder) / name
        if type(part['bytes']) is not int or part['bytes'] < 0:
            raise ValueError('invalid part size')
        if path.stat().st_size != part['bytes'] or sha256_file(path) != part['sha256']:
            raise ValueError('part checksum/size mismatch')
        yield path


def validate_header(header):
    if header.get('version') != VERSION or header.get('scope') != SCOPE:
        raise ValueError('unsupported feed version/scope; bootstrap v2')
    claimed = require_hash(header['generation'])
    if digest(encode({k: v for k, v in header.items() if k != 'generation'})) != claimed:
        raise ValueError('manifest checksum mismatch')
    if header['previous'] is not None:
        require_hash(header['previous'])
    if header['kind'] not in ('bootstrap', 'delta'):
        raise ValueError('invalid generation kind')
    require_date(header['cursor'])
    return header


def parquet_rows(paths, columns):
    # DuckDB reads only the allowlisted columns (no vectors/raw JSON). No corpus-sized Python list.
    import duckdb
    with closing(duckdb.connect()) as con:
        con.execute("SET memory_limit='512MB'")
        for path in paths:
            # Return UTC-naive datetime values so DuckDB does not require optional pytz.
            expressions = [(f'"{c}" AT TIME ZONE \'UTC\'' if c in
                            ('published_at', 'first_seen_at', 'removed_at_crawler') else f'"{c}"')
                           for c in columns]
            cursor = con.execute('SELECT ' + ','.join(expressions)
                                 + ' FROM read_parquet(?)', [str(path)])
            while batch := cursor.fetchmany(128):
                for values in batch:
                    yield dict(zip(columns, values))


def timestamp(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError('expected parquet timestamp')
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # normalized by parquet_rows
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def identity(row):
    if any(not isinstance(row.get(k), str) or not row[k] for k in ('ats', 'slug', 'id')):
        raise ValueError('invalid job identity')
    return f"{row['ats']}/{row['slug']}#{row['id']}"


def event(row, op):
    key = identity(row)
    if op == 'remove':
        reason = row['removal']
        if reason not in ('closed', 'left_dataset', 'unknown'):
            raise ValueError('invalid removal reason')
        return {'op': op, 'key': key, 'removal': reason,
                'removed_at_crawler': timestamp(row['removed_at_crawler'])}
    if row['is_open'] is not True:
        raise ValueError('bootstrap/upsert must come from an open-job export')
    job = {k: row[k] for k in FIELDS}
    # The upstream change key hashes coalesce(content, ''); mirror that equivalence.
    job['content'] = job['content'] or ''
    for k in ('published_at', 'first_seen_at'):
        job[k] = timestamp(job[k])
    return {'op': op, 'key': key, 'job': job}


def source_ref(side):
    return {'diff': side['from'] + '__' + side['to'],
            'content_sha256': require_hash(side['content_sha256'])}


def check_sidecar(side):
    if side.get('schema_version') != 2 or side.get('carry_done') is not True:
        raise ValueError('diff is not a completed v2 diff')
    if require_date(side['from']) >= require_date(side['to']):
        raise ValueError('diff dates must advance')
    if not CHANGE_KEY.issubset(side['change_key']):
        raise ValueError('diff does not track all projected mutable fields')
    full = side['parts']
    names = [p['file'] for p in full]
    if not full or len(names) != len(set(names)):
        raise ValueError('missing/duplicate full part descriptors')
    combined = '\n'.join(f"{p['file']} {require_hash(p['sha256'])}"
                         for p in sorted(full, key=lambda p: p['file']))
    if digest(combined.encode()) != side['content_sha256']:
        raise ValueError('source content digest mismatch')
    if not side['lite']['parts']:
        raise ValueError('missing lite parts')
    for op in ('added', 'changed', 'removed', 'carried'):
        if type(side['counts'][op]) is not int or side['counts'][op] < 0:
            raise ValueError('invalid source event count')


def store_event(db, row):
    try:
        db.execute('INSERT INTO events VALUES (?, ?)', (row['key'], encode(row).decode()))
    except sqlite3.IntegrityError as exc:
        raise ValueError('duplicate or conflicting job events') from exc


def load_delta(db, path, previous):
    side = read_json(path)
    check_sidecar(side)
    if side['from'] != previous['cursor'] or side['parent'] != previous['source']:
        raise ValueError('source gap or parent mismatch; re-bootstrap, never skip')
    folder = Path(path).with_suffix('') / 'lite'
    paths = list(part_paths(folder, side['lite']['parts'], 'parquet'))
    counts = Counter()
    columns = FIELDS + ('op', 'from_date', 'to_date', 'is_open', 'removal', 'removed_at_crawler')
    for row in parquet_rows(paths, columns):
        op = row['op']
        if op not in ('added', 'changed', 'removed', 'changed_prev', 'carried'):
            raise ValueError('unknown source operation')
        if row['from_date'] != side['from'] or row['to_date'] != side['to']:
            raise ValueError('row date differs from sidecar')
        key = identity(row)
        try:
            db.execute('INSERT INTO source_rows VALUES (?, ?)', (key, op))
        except sqlite3.IntegrityError as exc:
            raise ValueError('duplicate source operation') from exc
        counts[op] += 1
        if op in ('added', 'changed', 'removed'):
            store_event(db, event(row, 'remove' if op == 'removed' else 'upsert'))
    expected = dict(side['counts'], changed_prev=side['counts']['changed'])
    if any(counts[op] != expected[op] for op in expected):
        raise ValueError('incomplete source event counts')
    # Counts alone cannot establish that the previous and current versions refer to the same keys.
    if db.execute("SELECT key FROM source_rows GROUP BY key HAVING "
                  "(count(*) > 1 AND NOT (count(*) = 2 AND min(op) = 'changed' "
                  "AND max(op) = 'changed_prev')) OR "
                  "(count(*) = 1 AND min(op) IN ('changed','changed_prev')) LIMIT 1").fetchone():
        raise ValueError('conflicting source events or unpaired change')
    # Detect inputs changed during parsing, before producing any candidate head.
    list(part_paths(folder, side['lite']['parts'], 'parquet'))
    if read_json(path) != side:
        raise ValueError('sidecar changed during build')
    return {'cursor': side['to'], 'source': source_ref(side), 'source_sha256': digest(encode(side)),
            'kind': 'delta', 'snapshot_at': None}


def load_bootstrap(db, snapshot, index_path):
    snapshot = Path(snapshot).resolve()
    index = read_json(index_path)
    cursor = require_date(snapshot.name)
    if cursor < FIRST_DATE or index.get('head') != cursor:
        raise ValueError('bootstrap must match index head and be September 8 or later')
    manifest = read_json(snapshot / 'web/manifest.json')
    if not index.get('snapshot_built_at') or manifest['built_at'] != index['snapshot_built_at']:
        raise ValueError('bootstrap snapshot/index mismatch')
    anchors = [e for e in index['entries'] if e['to'] == cursor]
    if len(anchors) != 1:
        raise ValueError('bootstrap requires one published diff anchor at index head')
    anchor = anchors[0]
    source = source_ref(anchor)
    paths = sorted((snapshot / 'jobs').glob('*.parquet'))
    if not paths:
        raise ValueError('missing bootstrap parquet')
    hashes = [(p.name, sha256_file(p)) for p in paths]
    for row in parquet_rows(paths, FIELDS + ('is_open',)):
        store_event(db, event(row, 'upsert'))
    if db.execute('SELECT count(*) FROM events').fetchone()[0] != anchor['new_jobs_after_carry']:
        raise ValueError('incomplete bootstrap export count')
    if (sorted((snapshot / 'jobs').glob('*.parquet')) != paths
            or hashes != [(p.name, sha256_file(p)) for p in paths]
            or read_json(index_path) != index
            or read_json(snapshot / 'web/manifest.json') != manifest):
        raise ValueError('bootstrap inputs changed during build')
    return {'cursor': cursor, 'source': source, 'kind': 'bootstrap',
            'snapshot_at': manifest['built_at'], 'source_sha256': digest(encode(hashes))}


def write_generation(out, db, metadata, previous, scratch):
    pages, buffer, size = [], [], 0
    counts = {'upsert': 0, 'remove': 0}

    def flush():
        nonlocal buffer, size
        if buffer:
            data = b''.join(buffer)
            name = f'{len(pages):06d}.ndjson'
            (scratch / name).write_bytes(data)
            pages.append({'file': name, 'rows': len(buffer), 'bytes': len(data), 'sha256': digest(data)})
            buffer, size = [], 0

    for (payload,) in db.execute('SELECT payload FROM events ORDER BY key'):
        line = payload.encode('utf-8') + b'\n'
        if len(line) > MAX_BYTES:
            raise ValueError('job exceeds page byte limit')
        if len(buffer) >= MAX_ROWS or size + len(line) > MAX_BYTES:
            flush()
        buffer.append(line)
        size += len(line)
        counts[json.loads(payload)['op']] += 1
    flush()
    header = dict(metadata, version=VERSION, scope=SCOPE,
                  previous=previous['generation'] if previous else None, counts=counts, pages=pages)
    header['generation'] = digest(encode(header))
    destination = Path(out) / 'changes' / header['generation']
    destination.mkdir(parents=True, exist_ok=True)
    for page in pages:
        shutil.copyfile(scratch / page['file'], destination / page['file'])
    data = encode(header)
    (destination / 'manifest.json').write_bytes(data)
    # A candidate only: the last successfully published header must be retained separately.
    temporary = Path(out) / 'changes/latest.json.tmp'
    temporary.write_bytes(data)
    temporary.replace(Path(out) / 'changes/latest.json')
    return header


def build(out, *, diff=None, previous=None, snapshot=None, index=None):
    if bool(diff) == bool(snapshot) or (snapshot and not index) or (diff and not previous):
        raise ValueError('choose a diff with previous header, or snapshot with index')
    if previous:
        validate_header(previous)
    with tempfile.TemporaryDirectory(prefix='job-changes-') as tmp:
        scratch = Path(tmp)
        with closing(sqlite3.connect(scratch / 'events.sqlite')) as db:
            db.execute('CREATE TABLE events (key TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE source_rows (key TEXT, op TEXT, PRIMARY KEY(key,op))')
            metadata = load_delta(db, diff, previous) if diff else load_bootstrap(db, snapshot, index)
            if previous and metadata['cursor'] < previous['cursor']:
                raise ValueError('cannot move cursor backwards')
            if previous and metadata['cursor'] == previous['cursor'] and metadata['source'] != previous['source']:
                raise ValueError('same-date source was rewritten')
            return write_generation(out, db, metadata, previous, scratch)


def verified_rows(folder, header):
    validate_header(header)
    counts = Counter()
    for part, path in zip(header['pages'], part_paths(folder, header['pages'], 'ndjson')):
        if not 0 < part['rows'] <= MAX_ROWS or part['bytes'] > MAX_BYTES:
            raise ValueError('page exceeds bounds')
        lines = path.read_bytes().splitlines()
        if len(lines) != part['rows']:
            raise ValueError('page row count mismatch')
        for line in lines:
            row = json.loads(line)
            if row['op'] not in ('upsert', 'remove'):
                raise ValueError('invalid feed operation')
            if row['op'] == 'upsert' and row['key'] != identity(row['job']):
                raise ValueError('event key mismatch')
            counts[row['op']] += 1
            yield row
    if {op: counts[op] for op in ('upsert', 'remove')} != header['counts']:
        raise ValueError('generation count mismatch')


def apply_generation(db, folder, header, *, reset=False):
    """Reference transactional consumer; never commit a partial generation or skip a gap."""
    validate_header(header)
    db.execute('CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS checkpoint (singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation TEXT)')
    current = db.execute('SELECT generation FROM checkpoint WHERE singleton=1').fetchone()
    current = current[0] if current else None
    if current == header['generation']:
        return
    if header['kind'] == 'bootstrap':
        if current is not None and not reset:
            raise ValueError('bootstrap replaces state; explicit reset required')
    elif current is None or current != header['previous']:
        raise ValueError('consumer gap; re-bootstrap, never skip')
    with db:
        if header['kind'] == 'bootstrap':
            db.execute('DELETE FROM jobs')
        for row in verified_rows(folder, header):
            if row['op'] == 'upsert':
                db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?)', (row['key'], encode(row['job']).decode()))
            else:
                db.execute('DELETE FROM jobs WHERE key=?', (row['key'],))
        db.execute('INSERT OR REPLACE INTO checkpoint VALUES (1,?)', (header['generation'],))


def remote_head(base):
    url = base.rstrip('/') + '/data/changes/latest.json?check=' + uuid.uuid4().hex
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'Cache-Control': 'no-cache'}), timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def put(key, path):
    command = 'npx.cmd' if os.name == 'nt' else 'npx'
    subprocess.run([command, 'wrangler', 'r2', 'object', 'put', f'jobscream-data/{key}',
                    '--file', str(path), '--content-type',
                    'application/x-ndjson' if key.endswith('.ndjson') else 'application/json',
                    '--remote'], check=True)


def publish(out, header, base, get_head=remote_head, upload=put):
    validate_header(header)
    head = get_head(base)
    if head:
        validate_header(head)
    actual = head['generation'] if head else None
    if actual not in (header['previous'], header['generation']):
        raise ValueError('remote head differs from previous generation')
    folder = Path(out) / 'changes' / header['generation']
    if read_json(folder / 'manifest.json') != header:
        raise ValueError('local manifest changed after build')
    # Validate the whole generation before any upload; an invalid later page cannot leave a new head.
    for _ in verified_rows(folder, header):
        pass
    for page in header['pages']:
        upload(f"changes/{header['generation']}/{page['file']}", folder / page['file'])
    upload(f"changes/{header['generation']}/manifest.json", folder / 'manifest.json')
    if get_head(base) != head:
        raise ValueError('remote head changed during upload; serialize publishers')
    upload('changes/latest.json', folder / 'manifest.json')
    if get_head(base) != header:
        raise ValueError('publication readback mismatch; retry the same candidate')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--diff', type=Path, help='completed v2 diff sidecar, adjacent to its lite directory')
    group.add_argument('--snapshot', type=Path, help='completed export/YYYY-MM-DD for bootstrap/recovery')
    group.add_argument('--candidate', type=Path, help='retry a built manifest without rereading source exports')
    parser.add_argument('--index', type=Path, help='saved diffs/index.json matching the bootstrap export')
    parser.add_argument('--previous', type=Path, help='last successfully published feed manifest (not candidate latest)')
    parser.add_argument('--publish-base', help='opt in to R2 publication via the existing Worker')
    args = parser.parse_args()
    header = (validate_header(read_json(args.candidate)) if args.candidate else
              build(args.out, diff=args.diff, previous=read_json(args.previous) if args.previous else None,
                    snapshot=args.snapshot, index=args.index))
    if args.publish_base:
        publish(args.out, header, args.publish_base)
        # A durable success receipt, separate from changes/latest.json (which is only a candidate).
        receipt = args.out / 'published.json.tmp'
        receipt.write_bytes(encode(header))
        receipt.replace(args.out / 'published.json')
    print(json.dumps({'generation': header['generation'], 'cursor': header['cursor'],
                      'counts': header['counts'], 'published': bool(args.publish_base)}))


if __name__ == '__main__':
    main()
