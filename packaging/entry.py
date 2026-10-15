"""PyInstaller entry point. The same executable runs the GUI (default) or,
with ``--backend``, the background backend that the GUI launches."""

import multiprocessing
import sys

from discord_pro.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
