"""Compatibility shim for SparkVM CLI.

CLI implementation now lives in `src/cli/main.py`.
"""

from cli.main import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
