"""
Entry point for `python -m ecs` and for frozen builds (PyInstaller/Nuitka).

Important: PyInstaller executes the entry script as a top-level module, so
relative imports like `from .cli import app` can fail with:
  ImportError: attempted relative import with no known parent package
"""

import os
import sys

from ecs.cli import app

if __name__ == "__main__":
    # PowerShell completion works by launching `ecs` with special env vars.
    # If a completion invocation is interrupted, these can get stuck in the *parent shell*.
    # Then normal invocations may output nothing (because Click thinks it's in completion mode).
    #
    # Rules:
    # - If the user is executing a real command (argv has extra args), ALWAYS ignore completion env vars.
    # - If argv is just `ecs`, only keep completion env vars when it looks like a real completion subprocess.
    if len(sys.argv) > 1:
        os.environ.pop("_ECS_COMPLETE", None)
        os.environ.pop("_TYPER_COMPLETE_ARGS", None)
        os.environ.pop("_TYPER_COMPLETE_WORD_TO_COMPLETE", None)
    else:
        if os.getenv("_ECS_COMPLETE") and not (
            os.getenv("_TYPER_COMPLETE_ARGS") and os.getenv("_TYPER_COMPLETE_WORD_TO_COMPLETE")
        ):
            os.environ.pop("_ECS_COMPLETE", None)
            os.environ.pop("_TYPER_COMPLETE_ARGS", None)
            os.environ.pop("_TYPER_COMPLETE_WORD_TO_COMPLETE", None)

    # Force a stable program name so shell completion works consistently for:
    # - `ecs` (PowerShell resolves to ecs.exe)
    # - packaged binaries (which otherwise may become `ecs.exe`)
    app(prog_name="ecs")


