"""Entry point for the example application."""

from src.config import Config
from src.util.clock import now


def main() -> None:
    cfg = Config.load()
    print(f"started at {now()} with {cfg.name}")


if __name__ == "__main__":
    main()
