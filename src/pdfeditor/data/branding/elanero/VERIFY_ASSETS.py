"""Verify this extracted pack with Python 3 standard library; no network/writes."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import sys


def verify():
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / 'ASSET_MANIFEST.json').read_text(encoding='utf-8'))
    errors = []
    expected = {}
    for row in (root / 'SHA256SUMS.txt').read_text(encoding='utf-8').splitlines():
        digest, name = row.split('  ', 1)
        posix = PurePosixPath(name)
        if posix.is_absolute() or '..' in posix.parts or '\\' in name or ':' in name:
            raise ValueError('Unsafe manifest path: ' + name)
        if name in expected:
            raise ValueError('Duplicate manifest path: ' + name)
        expected[name] = digest
    for name, digest in expected.items():
        target = root / name
        if not target.resolve().is_relative_to(root):
            raise ValueError('Path outside pack: ' + name)
        if not target.is_file():
            errors.append('Missing: ' + name)
        elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            errors.append('Changed: ' + name)
    originals = manifest['files']
    if len(originals) != manifest['original_file_count'] or len(originals) != 21:
        errors.append('Unexpected original file count')
    for item in originals:
        name = item['path']
        if expected.get(name) != item['sha256']:
            errors.append('Original manifest mismatch: ' + name)
        target = root / name
        if name in expected and target.is_file() and target.stat().st_size != item['bytes']:
            errors.append('Wrong size: ' + name)
    if errors:
        print('\n'.join(errors))
        return 1
    print(f'PASS: {len(expected)} payload files match SHA-256; all 21 originals unchanged.')
    print('SHA256SUMS.txt is excluded from its own manifest. Extra files are ignored.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(verify())
    except Exception as exc:
        print('FAIL:', exc)
        sys.exit(1)
