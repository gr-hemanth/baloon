"""Worksheet inspection CLI runner.

Usage:
    python -m packages.worksheets.inspect <path_to_worksheet>
"""

import sys
from packages.worksheets.inspector import main

if __name__ == "__main__":
    main()
