"""Application configuration."""

from dataclasses import dataclass


@dataclass
class Config:
    name: str = "example"

    @classmethod
    def load(cls) -> "Config":
        return cls()
