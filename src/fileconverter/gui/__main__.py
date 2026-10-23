"""Allows `python -m fileconverter.gui`.

The guard matters: worker processes (multiprocessing "spawn", used for DOCX
conversion) import the main module again; without it they would start a second GUI.
Imports stay inside the guard so those processes do not load Qt at all.
"""

if __name__ == "__main__":
    import multiprocessing
    import sys

    multiprocessing.freeze_support()  # needed once the application is packaged

    from fileconverter.gui.app import main

    sys.exit(main())
