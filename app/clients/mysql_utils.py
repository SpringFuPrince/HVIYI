"""MySQL 异步 SQLAlchemy 客户端。

Client 只负责 Engine、AsyncSession 和事务生命周期；业务 CRUD 放在各层
Memory 中，使用 SQLAlchemy ``select``、``update``、``delete`` 等表达式。
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.sql.schema import MetaData

from app.conf.mysql_config import MySQLConfig, mysql_config
from app.utils.logger import logger


def _make_async_url(mysql_url: str) -> URL:
    """把普通 MySQL URL 规范化为 aiomysql 异步方言 URL。"""

    url = make_url(mysql_url)
    if url.drivername in {"mysql", "mysql+pymysql"}:
        return url.set(drivername="mysql+aiomysql")
    if url.drivername == "mysql+aiomysql":
        return url
    raise ValueError(
        "MYSQL_URL必须使用mysql://、mysql+pymysql://或mysql+aiomysql://"
    )


class MySQLClient:
    """进程级异步 MySQL Engine 和 Session 工厂。"""

    def __init__(self, config: MySQLConfig = mysql_config):
        self.config = config
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._lock = threading.Lock()

    def open(self) -> AsyncEngine:
        """延迟创建 Engine；真正连接发生在第一次数据库操作时。"""

        if self._engine is not None:
            return self._engine

        with self._lock:
            if self._engine is not None:
                return self._engine
            if not self.config.mysql_url:
                raise ValueError("缺少MYSQL_URL环境变量配置")

            url = _make_async_url(self.config.mysql_url)
            self._engine = create_async_engine(
                url,
                echo=self.config.echo_sql,
                pool_size=max(1, self.config.pool_size),
                max_overflow=max(0, self.config.max_overflow),
                pool_timeout=self.config.pool_timeout_seconds,
                pool_recycle=self.config.pool_recycle_seconds,
                pool_pre_ping=True,
                connect_args={"charset": "utf8mb4"},
            )
            self._session_factory = async_sessionmaker(
                bind=self._engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
            )
            logger.info("MySQL异步Engine和Session工厂已创建")
            return self._engine

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """返回共享 Session 工厂；每次调用工厂都会创建独立 AsyncSession。"""

        self.open()
        assert self._session_factory is not None
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """提供一个 Session；提交由业务方法显式决定。"""

        factory = self.session_factory()
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """提供自动提交/回滚的事务 Session。"""

        factory = self.session_factory()
        async with factory.begin() as session:
            yield session

    async def create_all(self, metadata: MetaData) -> None:
        """开发或测试环境建表；生产环境应改用 Alembic 迁移。"""

        engine = self.open()
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

    async def health_check(self) -> bool:
        """执行 SELECT 1；失败时返回 False。"""

        try:
            async with self.session() as session:
                return await session.scalar(text("SELECT 1")) == 1
        except Exception as exc:
            logger.warning(f"MySQL健康检查失败，原因：{exc}")
            return False

    def pool_stats(self) -> dict[str, Any]:
        if self._engine is None:
            return {"opened": False}

        pool = self._engine.sync_engine.pool
        result: dict[str, Any] = {"opened": True}
        for name in ("size", "checkedin", "checkedout", "overflow"):
            method = getattr(pool, name, None)
            if callable(method):
                result[name] = method()
        return result

    async def close(self) -> None:
        """关闭连接池；重复调用安全。"""

        with self._lock:
            engine = self._engine
            self._engine = None
            self._session_factory = None

        if engine is not None:
            await engine.dispose()
            logger.info("MySQL异步连接池已关闭")


_mysql_client: MySQLClient | None = None
_mysql_client_lock = threading.Lock()


def get_mysql_client() -> MySQLClient:
    """返回进程级 MySQL 客户端单例，调用时不会立即连接数据库。"""

    global _mysql_client
    if _mysql_client is None:
        with _mysql_client_lock:
            if _mysql_client is None:
                _mysql_client = MySQLClient()
    return _mysql_client


async def get_mysql_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求获得独立 AsyncSession。"""

    client = get_mysql_client()
    async with client.session() as session:
        yield session


async def close_mysql_client() -> None:
    """供 FastAPI lifespan/shutdown 调用。"""

    global _mysql_client
    with _mysql_client_lock:
        client = _mysql_client
        _mysql_client = None
    if client is not None:
        await client.close()
