#!/usr/bin/env python3
"""Build an allow-listed Skill archive without production data or credentials."""
import argparse
import hashlib
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / '.agents/skills/3d-sygg'
VERSION = 'v2.3.0'
REPORTS = {'audio-default-policy.md', 'image-api-validation.md', 'offline-tests.json', 'practical-repairs-20260929.md',
           'practical-repairs-tests.json', 'upgrade-validation.md'}


def source_files():
    yield SKILL / 'SKILL.md'
    for directory in ('agents', 'scripts', 'tests', 'references', 'config'):
        for path in sorted((SKILL / directory).rglob('*')):
            if not path.is_file() or path.is_symlink():
                continue
            if '__pycache__' in path.parts or path.name.startswith('.'):
                continue
            if directory == 'config' and not path.name.endswith('.example.json'):
                continue
            if path.suffix in {'.py', '.md', '.json', '.yaml'}:
                yield path
    for name in sorted(REPORTS):
        yield SKILL / 'reports' / name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / f'3d-sygg-{VERSION}.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as package:
        for path in source_files():
            package.write(path, '3d-sygg/' + str(path.relative_to(SKILL)))
        for name in ('README.md', 'LICENSE', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md'):
            content = (ROOT / name).read_text().replace('.agents/skills/3d-sygg/', '')
            package.writestr('3d-sygg/' + name, content)
        for path in sorted((ROOT / 'docs').rglob('*.md')):
            # ZIP is installed as a standalone Skill, unlike the repository layout.
            content = path.read_text().replace('../.agents/skills/3d-sygg/', '../')
            package.writestr('3d-sygg/docs/' + str(path.relative_to(ROOT / 'docs')), content)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (args.output / 'SHA256SUMS.txt').write_text(f'{checksum}  {archive.name}\n')
    print(f'{archive}: {checksum}')


if __name__ == '__main__':
    main()
