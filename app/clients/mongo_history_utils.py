"""兼容旧导入路径；新代码请改用 ``app.clients.mongo_utils``。

L1 已迁移到 MySQL，本模块不再提供 MongoDB 聊天记录 CRUD。
"""

from app.clients.mongo_utils import (
    MongoMemoryClient,
    close_mongo_memory_client,
    get_mongo_memory_client,
)

__all__ = [
    "MongoMemoryClient",
    "close_mongo_memory_client",
    "get_mongo_memory_client",
]
