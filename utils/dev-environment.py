#!/usr/bin/env python3
"""Command-line entry point; implementation is importable for offline tests."""

import sys

from dev_environment import main


if __name__ == "__main__":
    sys.exit(main())
