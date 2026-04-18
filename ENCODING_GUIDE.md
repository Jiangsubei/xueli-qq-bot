# Encoding Guide

## What was added

This repository now includes these encoding safeguards:

- `.editorconfig` to keep text files on UTF-8 by default
- `scripts/use_utf8_terminal.ps1` to switch a PowerShell session into UTF-8 mode
- `scripts/check_utf8.py` to scan repository text files and report non-UTF-8 files
- `.githooks/pre-commit` to block commits that introduce non-UTF-8 text files
- `.github/workflows/utf8-check.yml` to enforce the same check in CI

## Recommended workflow on Windows

In PowerShell, run this from the repository root before editing files that may contain Chinese text:

```powershell
. .\scripts\use_utf8_terminal.ps1
```

Important: use the leading dot and space so the script affects the current shell session.

## Enable the local Git hook

Run this once in the repository root:

```powershell
.\scripts\install_git_hooks.ps1
```

This configures `git` to use `.githooks` for this repository so the UTF-8 check runs automatically before each commit.

## How to verify files manually

Run:

```powershell
python .\scripts\check_utf8.py .
```

The command exits with code `1` if a tracked text file cannot be decoded as UTF-8.

## Safer file-editing habits

- Prefer direct editor changes over large terminal here-strings with Chinese content.
- If you must write files from PowerShell, use `Set-Content -Encoding UTF8` or `Out-File -Encoding utf8`.
- For code that is frequently rewritten by scripts, ASCII plus Unicode escapes is safer than raw non-ASCII text.
- Keep UI structure stable and change data sources first when possible.
