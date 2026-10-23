"""Allows `python -m fileconverter ...`.

Guarded because worker processes (multiprocessing "spawn") import the main module again.
"""

if __name__ == "__main__":
    import multiprocessing
    import sys

    multiprocessing.freeze_support()

    from fileconverter.cli import main

    sys.exit(main())
