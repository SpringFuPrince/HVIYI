from dataclasses import dataclass
import os
from dotenv import load_dotenv


load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)# 创建以后不允许随意修改
class RedisConfig:
    """Redis 配置类"""
    host: str | None
    port: int | None
    db: int | None
    enabled: bool
    key_prefix: str
    socket_connect_timeout_seconds: float
    socket_timeout_seconds: float
    l1_ttl_seconds: int
    l2_ttl_seconds: int
    l4_ttl_seconds: int


redis_config = RedisConfig(
    host=os.getenv("REDIS_HOST"),
    port=int(os.getenv("REDIS_PORT")),
    db=int(os.getenv("REDIS_DB")),
    enabled=_env_bool("REDIS_MEMORY_CACHE_ENABLED", True),
    key_prefix=os.getenv("REDIS_MEMORY_CACHE_PREFIX", "hviyi"),
    socket_connect_timeout_seconds=float( os.getenv("REDIS_CONNECT_TIMEOUT", "1.0")),
    socket_timeout_seconds=float(os.getenv("REDIS_SOCKET_TIMEOUT", "1.0")),
    l1_ttl_seconds=int(os.getenv("REDIS_MEMORY_L1_TTL", "1800")),
    l2_ttl_seconds=int(os.getenv("REDIS_MEMORY_L2_TTL", "3600")),
    l4_ttl_seconds=int(os.getenv("REDIS_MEMORY_L4_TTL", "1800")),
)
