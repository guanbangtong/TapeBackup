"""Entry point (also what PyInstaller packages): python run.py"""

import sys

from tape_backup.app import main

if __name__ == "__main__":
    sys.exit(main())
