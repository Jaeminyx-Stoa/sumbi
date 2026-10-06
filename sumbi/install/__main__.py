"""Preserve the standalone install command."""

from sumbi.cli.install import main

if __name__ == "__main__":
    raise SystemExit(main())
