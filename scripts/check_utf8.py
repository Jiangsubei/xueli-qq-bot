from __future__ import annotations

import argparse
from pathlib import Path

TEXT_SUFFIXES = {
    '.py', '.html', '.css', '.js', '.md', '.json', '.toml', '.yaml', '.yml',
    '.txt', '.ini', '.cfg', '.ps1', '.bat', '.sh'
}
SKIP_DIRS = {
    '.git', 'venv', '__pycache__', '.mypy_cache', '.pytest_cache',
    '.ruff_cache', 'node_modules', '.idea', '.vscode'
}


def is_probably_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES


def can_decode_utf8(path: Path) -> tuple[bool, str | None]:
    data = path.read_bytes()
    try:
        data.decode('utf-8')
        return True, None
    except UnicodeDecodeError:
        try:
            data.decode('utf-8-sig')
            return True, None
        except UnicodeDecodeError as exc:
            return False, str(exc)


def iter_files(root: Path):
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if is_probably_text(path):
            yield path


def main() -> int:
    parser = argparse.ArgumentParser(description='Check that repository text files decode as UTF-8.')
    parser.add_argument('root', nargs='?', default='.', help='Root directory to scan')
    args = parser.parse_args()

    root = Path(args.root).resolve()
    failures: list[tuple[Path, str]] = []
    checked = 0

    for path in iter_files(root):
        checked += 1
        ok, error = can_decode_utf8(path)
        if not ok:
            failures.append((path, error or 'unknown decode error'))

    if failures:
        print(f'UTF-8 check failed. {len(failures)} file(s) could not be decoded as UTF-8:')
        for path, error in failures:
            print(f'- {path}: {error}')
        return 1

    print(f'UTF-8 check passed. Checked {checked} text file(s).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
