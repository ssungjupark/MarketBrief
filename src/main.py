import asyncio

from macro_pulse.app.cli import main


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
