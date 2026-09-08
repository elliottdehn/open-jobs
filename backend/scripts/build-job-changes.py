#!/usr/bin/env python3
"""Build/publish a public snapshot change feed. Standard library only; see JOB-CHANGES.md."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid

FIELDS = ('ats', 'slug', 'id', 'title', 'company', 'location', 'url', 'seen', 'pub', 'jd')
MAX_ROWS = 1000
MAX_BYTES = 4 * 1024 * 1024


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_snapshot(db, table, web):
    """One group in memory at a time; stale, unreferenced group files are ignored."""
    manifest = read_json(web / 'manifest.json')
    leaves = [n for n in manifest['tree'] if not n['children']]
    ids = [n['id'] for n in leaves]
    if (any(type(i) is not int or i < 0 for i in ids) or len(set(ids)) != len(ids)
            or len(ids) != manifest['leaves'] or not ids):
        raise ValueError('invalid or empty manifest leaves')
    count = 0
    for leaf in leaves:
        group = read_json(web / 'groups' / f"{leaf['id']}.json")
        if group['leaf'] != leaf['id'] or len(group['jobs']) != leaf['size']:
            raise ValueError('incomplete or mismatched group')
        for job in group['jobs']:
            if any(not isinstance(job.get(k), str) or not job[k] for k in ('ats', 'slug', 'id')):
                raise ValueError('invalid job identity')
            key = f"{job['ats']}/{job['slug']}#{job['id']}"
            payload = encode({k: job[k] for k in FIELDS if k in job}).decode('utf-8')
            # Duplicate identities fail closed rather than emitting ambiguous removals/updates.
            db.execute(f'INSERT INTO {table} VALUES (?, ?)', (key, payload))
            count += 1
    if count != manifest['jobs'] or count != sum(n['size'] for n in leaves):
        raise ValueError('incomplete snapshot job count')
    db.commit()
    return manifest


def snapshot_digest(db, table):
    checksum = hashlib.sha256()
    for key, payload in db.execute(f'SELECT key,payload FROM {table} ORDER BY key'):
        checksum.update(encode([key, payload]) + b'\n')
    return checksum.hexdigest()


def build(web, previous_web=None):
    web = Path(web).resolve()
    previous_web = Path(previous_web).resolve() if previous_web else None
    if web == previous_web:
        raise ValueError('previous snapshot must be a separate preserved directory')
    previous = read_json(previous_web / 'changes/latest.json') if previous_web else None
    if previous and previous.get('version') != 1:
        raise ValueError('unsupported previous feed version')
    with tempfile.TemporaryDirectory(prefix='job-changes-') as scratch:
        scratch = Path(scratch)
        with closing(sqlite3.connect(scratch / 'jobs.sqlite')) as db:
            for table in ('old', 'new'):
                db.execute(f'CREATE TABLE {table} (key TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            if previous_web:
                old_manifest = load_snapshot(db, 'old', previous_web)
                if old_manifest['built_at'] != previous['snapshot_at']:
                    raise ValueError('previous groups no longer match their feed snapshot')
                if snapshot_digest(db, 'old') != previous['snapshot_sha256']:
                    raise ValueError('previous snapshot content changed after publication')
            manifest = load_snapshot(db, 'new', web)
            if previous and manifest['built_at'] <= previous['snapshot_at']:
                raise ValueError('snapshot must advance beyond the previous published snapshot')
            pages, buffer, size = [], [], 0

            def flush():
                nonlocal buffer, size
                if not buffer:
                    return
                data = b''.join(buffer)
                name = f'{len(pages):06d}.ndjson'
                (scratch / name).write_bytes(data)
                pages.append({'file': name, 'rows': len(buffer), 'bytes': len(data), 'sha256': digest(data)})
                buffer, size = [], 0

            counts = {'upsert': 0, 'remove': 0}
            queries = (
                ('upsert', 'SELECT n.key,n.payload FROM new n LEFT JOIN old o ON o.key=n.key '
                 'WHERE o.key IS NULL OR n.payload != o.payload ORDER BY n.key'),
                ('remove', 'SELECT o.key,NULL FROM old o LEFT JOIN new n ON n.key=o.key '
                 'WHERE n.key IS NULL ORDER BY o.key'),
            )
            for op, query in queries:
                for key, payload in db.execute(query):
                    row = {'op': op, 'key': key}
                    if payload is not None:
                        row['job'] = json.loads(payload)
                    line = encode(row) + b'\n'
                    if len(line) > MAX_BYTES:
                        raise ValueError(f'job exceeds page byte limit: {key}')
                    if len(buffer) >= MAX_ROWS or size + len(line) > MAX_BYTES:
                        flush()
                    buffer.append(line)
                    size += len(line)
                    counts[op] += 1
            flush()
            snapshot_sha256 = snapshot_digest(db, 'new')
        header = {'version': 1, 'snapshot_at': manifest['built_at'], 'jobs': manifest['jobs'],
                  'snapshot_sha256': snapshot_sha256,
                  'previous': previous['generation'] if previous else None,
                  'counts': counts, 'pages': pages}
        generation = digest(encode(header))
        header['generation'] = generation
        destination = web / 'changes' / generation
        destination.mkdir(parents=True, exist_ok=True)
        for page in pages:
            shutil.copyfile(scratch / page['file'], destination / page['file'])
        data = encode(header)
        (destination / 'manifest.json').write_bytes(data)
        # This is a local candidate, not evidence of publication. Publish last remotely.
        (web / 'changes/latest.json').write_bytes(data)
        return header


def remote_head(base):
    # The unique query avoids intermediary caches of the mutable pointer.
    url = base.rstrip('/') + '/data/changes/latest.json?check=' + uuid.uuid4().hex
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'Cache-Control': 'no-cache'}),
                                    timeout=60) as response:
            return json.load(response)
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


def publish(web, header, base, get_head=remote_head, upload=put):
    """Single publisher only. Reject a stale parent; never advance head after an upload failure."""
    head = get_head(base)
    actual = head['generation'] if head else None
    if actual not in (header['previous'], header['generation']):
        raise ValueError('remote head differs from previous snapshot; recover the last published export')
    folder = Path(web) / 'changes' / header['generation']
    for page in header['pages']:
        data = (folder / page['file']).read_bytes()
        if len(data) != page['bytes'] or digest(data) != page['sha256']:
            raise ValueError('page changed after build')
        upload(f"changes/{header['generation']}/{page['file']}", folder / page['file'])
    upload(f"changes/{header['generation']}/manifest.json", folder / 'manifest.json')
    # Detect an unexpected competing run before changing the pointer; not a distributed lock.
    if get_head(base) != head:
        raise ValueError('remote head changed during upload; serialize publishers and retry')
    upload('changes/latest.json', folder / 'manifest.json')
    if get_head(base) != header:
        raise ValueError('publication readback did not match; retain this export and inspect before retrying')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web', type=Path, required=True)
    parser.add_argument('--previous-web', type=Path)
    parser.add_argument('--publish-base', help='opt in to R2 publication; URL of the Worker serving that bucket')
    args = parser.parse_args()
    header = build(args.web, args.previous_web)
    if args.publish_base:
        publish(args.web, header, args.publish_base)
    print(json.dumps({'generation': header['generation'], 'counts': header['counts'],
                      'pages': len(header['pages']), 'published': bool(args.publish_base)}))


if __name__ == '__main__':
    main()
