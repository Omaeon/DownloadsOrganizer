"""Entry script of the PyInstaller build (``DownloadsOrganizer.spec``).

The same as ``python -m downloads_organizer``. It lives outside the package so that the
build does not put the package folder itself on the module search path.
"""

from downloads_organizer.gui import main

if __name__ == "__main__":
    main()
