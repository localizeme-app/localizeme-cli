"""Lets the CLI run as `python -m localizeme_cli`, not only via the console script."""

from .main import main

if __name__ == '__main__':
    main()
