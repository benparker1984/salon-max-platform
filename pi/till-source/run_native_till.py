from __future__ import annotations

import sys


def main():
    try:
        from native_till_app.app import run
    except ImportError as exc:
        missing = str(exc)
        print("Native till app could not start.")
        print()
        print("Most likely PySide6 or PyQt6 is not installed yet.")
        print("Install it with:")
        print("    sudo apt install -y python3-pyqt6")
        print()
        print(f"Import error: {missing}")
        return 1

    return run()


if __name__ == "__main__":
    sys.exit(main())

