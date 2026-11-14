import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path


@dataclass(frozen=True)
class Config:
    root: Path
    timeout: float = 25
    interval: float = 1
    max_bytes: int = 24 * 1024 * 1024
    freshness_days: int = 7
    user_agent: str = "sp-mcp/0.1 (+public normative document research)"

    def __post_init__(self):
        if not 1 <= self.timeout <= 120 or not 0.1 <= self.interval <= 60:
            raise ValueError("Timeout must be 1..120 s; interval 0.1..60 s")
        if not 1 <= self.freshness_days <= 90:
            raise ValueError("Freshness must be 1..90 days")

    @classmethod
    def from_env(cls):
        return cls(
            root=Path(os.getenv("SP_MCP_DATA_DIR") or user_data_path("sp-mcp", appauthor=False)),
            timeout=float(os.getenv("SP_MCP_TIMEOUT", "25")),
            interval=float(os.getenv("SP_MCP_REQUEST_INTERVAL", "1")),
            freshness_days=int(os.getenv("SP_MCP_FRESHNESS_DAYS", "7")),
        )
