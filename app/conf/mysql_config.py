from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MySQLConfig:
    mysql_url: str | None
    pool_size: int
    max_overflow: int
    pool_timeout_seconds: float
    pool_recycle_seconds: int
    echo_sql: bool


mysql_config = MySQLConfig(
    # 推荐：mysql+aiomysql://用户名:密码@主机:3306/数据库名?charset=utf8mb4
    mysql_url=os.getenv("MYSQL_URL") or os.getenv("DATABASE_URL"),
    pool_size=int(os.getenv("MYSQL_POOL_SIZE", "5")),
    max_overflow=int(os.getenv("MYSQL_MAX_OVERFLOW", "10")),
    pool_timeout_seconds=float(os.getenv("MYSQL_POOL_TIMEOUT", "10")),
    pool_recycle_seconds=int(os.getenv("MYSQL_POOL_RECYCLE", "1800")),
    echo_sql=_env_bool("MYSQL_ECHO_SQL"),
)
