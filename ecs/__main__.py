"""
Entry point for `python -m ecs` and for frozen builds (PyInstaller/Nuitka).

Important: PyInstaller executes the entry script as a top-level module, so
relative imports like `from .cli import app` can fail with:
  ImportError: attempted relative import with no known parent package
"""

from ecs.cli import app

if __name__ == "__main__":
    # Force a stable program name so shell completion works consistently for:
    # - `ecs` (PowerShell resolves to ecs.exe)
    # - packaged binaries (which otherwise may become `ecs.exe`)
    app(prog_name="ecs")


