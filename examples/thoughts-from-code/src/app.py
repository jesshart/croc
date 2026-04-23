"""Entry point for the example application."""

from src.config import Config
from src.util.clock import now


def persist_parquet(name: str) -> None:
    """Stub for the example — croc attack scans for this call pattern."""
    _ = name


def main() -> None:
    cfg = Config.load()
    print(f"started at {now()} with {cfg.name}")
    persist_parquet("app_output")


if __name__ == "__main__":
    main()
