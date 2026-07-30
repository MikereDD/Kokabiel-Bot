#!/usr/bin/env python3
"""Kokabiel — Film, TV & Documentary Guide.

Database and log locations are read from ~/bots/config/kokabielrc.py.
The configured values may use $HOME or ~ and are resolved during startup.
"""

from app import main


if __name__ == "__main__":
    main()
