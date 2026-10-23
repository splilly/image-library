"""imagelib: catalog, verify and de-duplicate photo libraries spread over many drives."""

__version__ = "0.1.0"


def main() -> None:
    import multiprocessing
    import sys

    multiprocessing.freeze_support()  # required for worker processes in packaged builds
    from .cli import main as cli_main

    sys.exit(cli_main())
