#!/usr/bin/env python3
"""Run the current cleaned Jurassic Park Builder private server."""

from jpb_server.current_server import main


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("JPB server stopped")
