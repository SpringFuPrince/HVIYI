"""L2 记忆使用的异步 MongoDB 客户端。"""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from app.conf.mongo_config import MongoConfig, mongo_config
from app.utils.logger import logger


class MongoMemoryClient:
    """只管理 AsyncMongoClient、集合和索引，不承载 L2 业务逻辑。"""

    def __init__(self, config: MongoConfig = mongo_config):
        self.config = config
        self._client: AsyncMongoClient[dict[str, Any]] | None = None
        self._database: AsyncDatabase[dict[str, Any]] | None = None

    def open(self) -> AsyncDatabase[dict[str, Any]]:
        """延迟创建客户端；实际网络连接发生在第一次数据库操作时。"""

        if self._database is not None:
            return self._database

        if not self.config.mongo_url:
            raise ValueError("缺少MONGO_URL环境变量配置")
        if not self.config.database_name:
            raise ValueError("缺少MONGO_DB_NAME环境变量配置")

        self._client = AsyncMongoClient(
            self.config.mongo_url,
            serverSelectionTimeoutMS=self.config.server_selection_timeout_ms,
            connectTimeoutMS=self.config.connect_timeout_ms,
            maxPoolSize=max(1, self.config.max_pool_size),
            tz_aware=True,
        )
        self._database = self._client[self.config.database_name]
        logger.info("MongoDB异步客户端已创建")
        return self._database

    @property
    def meeting_episodes(self) -> AsyncCollection[dict[str, Any]]:
        """每场会议只对应一个动态会议记忆文档。"""

        return self.open()[self.config.meeting_collection]

    @property
    def profile_attributes(self) -> AsyncCollection[dict[str, Any]]:
        """本机用户画像集合；一个文档只代表一个属性。"""

        return self.open()[self.config.profile_attribute_collection]

    async def initialize_indexes(self) -> None:
        """应用启动时调用一次，索引创建本身是幂等的。"""

        await self.meeting_episodes.create_index(
            [("meeting_id", ASCENDING)],
            unique=True,
            name="uq_meeting_memory_id",
        )
        await self.profile_attributes.create_index(
            [("attribute_key", ASCENDING)],
            unique=True,
            name="uq_local_profile_attribute_key",
        )

    async def health_check(self) -> bool:
        try:
            await self.open().command({"ping": 1})
            return True
        except Exception as exc:
            logger.warning(f"MongoDB健康检查失败，原因：{exc}")
            return False

    async def close(self) -> None:
        client = self._client
        self._client = None
        self._database = None
        if client is not None:
            await client.close()
            logger.info("MongoDB异步客户端已关闭")


_mongo_memory_client: MongoMemoryClient | None = None


def get_mongo_memory_client() -> MongoMemoryClient:
    """返回供 FastAPI 单一事件循环使用的进程级客户端。"""

    global _mongo_memory_client
    if _mongo_memory_client is None:
        _mongo_memory_client = MongoMemoryClient()
    return _mongo_memory_client


async def close_mongo_memory_client() -> None:
    global _mongo_memory_client
    client = _mongo_memory_client
    _mongo_memory_client = None
    if client is not None:
        await client.close()
