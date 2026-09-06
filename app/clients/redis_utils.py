"""记忆缓存使用的异步 Redis 客户端。"""

import threading
import time
from redis.asyncio import Redis
from app.conf.redis_config import RedisConfig, redis_config
from app.utils.logger import logger


class MemoryCache:
    """
    记忆缓存
    """

    def __init__(self, config: RedisConfig = redis_config):
        self._redis_config = config
        self._redis_client: Redis | None = None
        self._lock = threading.Lock()
        self._retry_after = 0.0
        # 当Redis不可用时，先把需要删除的缓存规则记下来、等Redis恢复再删除的集合
        self._pending_cache_delete: set[str] = set()


    @property
    def is_configured(self) -> bool:
        """检查是否配置了Redis_HOST和是否启用记忆缓存。"""
        return bool(self._redis_config.enabled and self._redis_config.host and self._redis_config.port)

    @property
    def is_available(self) -> bool:
        """
        检查Redis是否可使用
        配置了且未熔断
        """
        return self.is_configured and time.monotonic() >= self._retry_after

    def mark_unavailable(self, cooldown_seconds: float = 30.0) -> None:
        """Redis启动熔断，防止重复等待连接超时"""
        # 记录熔断时间 ：当前时间 + 冷却时间
        self._retry_after = time.monotonic() + cooldown_seconds

    def mark_available(self) -> None:
        """Redis可用后，清除熔断状态"""
        self._retry_after = 0.0

    def defer_cache_delete(self, keys: list[str]) -> None:
        """Redis暂不可用时，把需要删除的缓存规则保存起来，等Redis可用后再删除"""

        with self._lock:
            self._pending_cache_delete.update(keys)

    def get_redis_client(self) -> Redis:
        """创建异步 Redis 客户端"""
        if not self.is_available:
            raise RuntimeError("Redis记忆缓存未启用或缺少REDIS_URL")
        if self._redis_client is not None:
            return self._redis_client

        with self._lock:
            if self._redis_client is None:
                self._redis_client = Redis(
                    host=self._redis_config.host,
                    port=self._redis_config.port,
                    db=self._redis_config.db,
                    decode_responses=True,
                    socket_connect_timeout=self._redis_config.socket_connect_timeout_seconds,
                    socket_timeout=self._redis_config.socket_timeout_seconds,
                )
                logger.info("Redis记忆缓存客户端已创建")
        return self._redis_client

    def _build_redis_key(self, logical_key: str) -> str:
        """给逻辑 Key加上项目统一前缀"""
        # 项目统一前缀
        prefix = self._redis_config.key_prefix

        return f"{prefix}:{logical_key}"


    async def get_cache(self, key: str) -> str | None:
        """
        获取缓存
        """
        client = self.get_redis_client()
        # 清理Redis故障时积累的缓存规则
        await self._delete_deferred_cache(client)
        value = await client.get(self._build_redis_key(key))
        # 标记Redis可用
        self.mark_available()
        return value


    async def set_cache(self, key: str, value: str, ttl_seconds: int) -> None:
        """
        设置缓存
        """
        client = self.get_redis_client()
        # 清理Redis故障时积累的缓存规则
        await self._delete_deferred_cache(client)
        await client.set(
            self._build_redis_key(key),
            value,
            ex=ttl_seconds,
        )
        # 标记Redis可用
        self.mark_available()


    async def delete_cache(self, patterns: list[str]) -> int:
        """
        删除指定缓存，并把Redis故障时积累的一起清理
        """

        client = self.get_redis_client()
        with self._lock:
            # 合并待删除的缓存规则和指定的缓存规则
            all_patterns = list(self._pending_cache_delete.union(patterns))

        deleted_count = await self._delete_matched_cache(client, all_patterns)
        with self._lock:
            # 删除pending_cache_delete已成功删掉的数据
            self._pending_cache_delete.difference_update(all_patterns)
        # 标记Redis可用
        self.mark_available()
        return deleted_count

    async def _delete_deferred_cache(self, client: Redis) -> None:
        """
        清理Redis故障时积累的缓存规则
        """
        with self._lock:
            patterns = list(self._pending_cache_delete)
        if not patterns:
            return

        await self._delete_matched_cache(client, patterns)
        with self._lock:
            self._pending_cache_delete.difference_update(patterns)

    async def _delete_matched_cache(self, client: Redis, patterns: list[str]) -> int:
        """
        根据缓存规则删除缓存
        """
        deleted_count = 0
        for pattern in dict.fromkeys(patterns):
            physical_pattern = self._build_redis_key(pattern)
            keys: list[str] = []
            async for key in client.scan_iter(match=physical_pattern, count=100):
                keys.append(key)
                if len(keys) >= 100:
                    deleted_count += int(await client.delete(*keys))
                    keys.clear()
            if keys:
                deleted_count += int(await client.delete(*keys))
        return deleted_count

    async def ping(self) -> bool:
        if not self.is_available:
            return False
        result = bool(await self.get_redis_client().ping())
        self.mark_available()
        return result

    async def close(self) -> None:
        with self._lock:
            client = self._redis_client
            self._redis_client = None
        if client is not None:
            await client.aclose()
            logger.info("Redis记忆缓存客户端已关闭")


_redis_memory_cache: MemoryCache | None = None
_redis_memory_cache_lock = threading.Lock()


def get_redis_memory_cache() -> MemoryCache:
    """
    获取Redis记忆缓存实例
    """
    global _redis_memory_cache
    if _redis_memory_cache is None:
        with _redis_memory_cache_lock:
            if _redis_memory_cache is None:
                _redis_memory_cache = MemoryCache()
    return _redis_memory_cache


async def initialize_redis_memory_cache() -> None:
    """
    初始化Redis记忆缓存
    """
    cache = get_redis_memory_cache()
    if not cache.is_available:
        logger.info("Redis记忆缓存未启用，记忆召回将直接查询数据库")
        return
    try:
        await cache.ping()
        logger.info("Redis记忆缓存连接正常")
    except Exception as exc:
        # Redis 只承担缓存职责，连接失败不能阻止主服务启动。
        cache.mark_unavailable()
        logger.warning(f"Redis记忆缓存连接失败，将回退数据库：{exc}")


async def close_redis_memory_cache() -> None:
    """
    关闭Redis记忆缓存
    """
    global _redis_memory_cache
    with _redis_memory_cache_lock:
        cache = _redis_memory_cache
        _redis_memory_cache = None
    if cache is not None:
        await cache.close()
