"""Create a single-use invitation for the configured recognition database."""

import argparse
from pathlib import Path

from fingerprint_server.recognition.history import History


def main() -> None:
    """Print a new invitation to the operator's terminal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    history = History(args.database)
    history.initialize()
    print(history.invite())


if __name__ == "__main__":
    main()
