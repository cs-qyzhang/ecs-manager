# Repository Guidelines

## Project Structure & Module Organization

- `ecs/`: main Python package (Typer-based CLI). Entrypoint is `ecs.cli:app` (see `pyproject.toml`).
- `scripts/`: packaging/build helpers (PyInstaller + Nuitka) for Windows/macOS.
- `env.example`: template for local configuration; copy to `.env` when running locally.
- `requirements.txt`: runtime dependencies; `requirements-build.txt`: build-only deps.
- `dist/`, `dist_nuitka/`, `build/`, `*.egg-info/`: generated artifacts—do not edit; delete/regenerate if needed.

## Build, Test, and Development Commands

- Set up a local environment (preferred): `uv venv --python 3.12` then `uv sync` then `uv pip install -e .`
- Run the CLI: `ecs --help` (or `python -m ecs --help` without installing).
- Windows build (PyInstaller): `.\scripts\build_windows.ps1 -Mode onedir` (recommended) or `-Mode onefile`.
- Windows build (Nuitka): `.\scripts\build_windows_nuitka.ps1 -SyncDeps` (first time) then `.\scripts\build_windows_nuitka.ps1` (incremental); add `-Clean` to force rebuild.
- macOS build scripts: `./scripts/build_macos.sh` and `./scripts/build_macos_nuitka.sh` (see `README.md` for examples).

## Coding Style & Naming Conventions

- Python: 4-space indentation, type hints, and `from __future__ import annotations` (match existing modules).
- Keep CLI startup behavior intact: `ecs/cli.py` loads `.env` early so Typer/Click envvar options work—avoid refactors that move that earlier/later without intent.
- Naming: modules/functions `snake_case`; CLI commands follow Typer conventions (function name → command name).

## Testing Guidelines

- No first-party test suite is configured yet.
- For changes, run a quick sanity check: `python -m compileall ecs` and smoke-test the touched command (e.g., `ecs --help`, `ecs config --help`).

## Commit & Pull Request Guidelines

- Commit subjects in this repo are short, imperative, and lowercase (e.g., `add port command`).
- PRs should include: what changed, example CLI commands/output, and updates to `README.md`/`env.example` when adding flags or env vars. Note platform constraints if touching `scripts/` (Windows/macOS builds are not cross-compiled).

## Security & Configuration Tips

- Never commit secrets. Use `env.example` → `.env` locally and set credentials via environment variables when possible.
- If `.env` was ever committed, remove it from the index (`git rm --cached .env`) and rotate any exposed keys.

## Agent-Specific Notes

- If you add third-party Python dependencies, use `uv` (keep changes local to `.venv`) and commit any resulting `uv.lock` updates.

