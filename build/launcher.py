"""PyInstaller entry point (src.app uses package-relative imports)."""
import sys

from src.app import main

if __name__ == "__main__":
    sys.exit(main())
