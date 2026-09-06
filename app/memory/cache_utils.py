"""异步 Cache-Aside 查询和数据库写后缓存失效装饰器。"""

from __future__ import annotations

import hashlib
import json
from functools import wraps
from typing import Any

from pydantic import BaseModel

from app.clients.redis_utils import get_redis_memory_cache
from app.utils.logger import logger


def cache_key_part(value: Any) -> str:
    """
    把业务ID转换成固定长度的部分缓存key
    """
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]


def build_l1_summary_cache_key(meeting_id: str, session_id: str) -> str:
    """
    会话滚动摘要缓存key
    """
    return f"memory:l1:{cache_key_part(meeting_id)}:{cache_key_part(session_id)}:summary"


def build_l2_episode_cache_key(meeting_id: str) -> str:
    """
    会议情节记忆缓存key
    """
    return f"memory:l2:episode:{cache_key_part(meeting_id)}"


def build_l2_profile_cache_key() -> str:
    """
    用户画像记忆缓存key
    """
    return "memory:l2:profile"


def build_l4_progress_cache_key(meeting_id: str, recent_limit: int) -> str:
    """
    任务进度记忆缓存key
    """
    return f"memory:l4:{cache_key_part(meeting_id)}:progress:{recent_limit}"


def build_l4_task_cache_target(meeting_id: str) -> list[str]:
    """
    任务进度待删除的记忆缓存key
    """
    return [f"memory:l4:{cache_key_part(meeting_id)}:progress:*"]


def serialize_cache(value: Any) -> str:
    """
    序列化缓存
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def cache_aside(
    *,
    key_builder,    # 缓存key构建函数
    ttl_seconds: int,    # 缓存过期时间
    decoder=None,    # 缓存值解码函数
):
    """
    缓存旁路策略装饰器
    Redis 命中时直接返回，未命中时执行原查询并缓存Redis
    """

    if ttl_seconds <= 0:
        raise ValueError("缓存TTL必须大于0")

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            key = key_builder(*args, **kwargs)
            cache = get_redis_memory_cache()
            # 缓存key为空或Redis不可用，直接返回原函数结果,使用数据库查询
            if not key or not cache.is_available:
                return await func(*args, **kwargs)

            try:
                cached_json = await cache.get_cache(key)
                if cached_json is not None:
                    cached_data = json.loads(cached_json)
                    logger.debug(f"记忆缓存命中：{key}")
                    return decoder(cached_data) if decoder else cached_data
            except Exception as exc:
                cache.mark_unavailable()
                logger.warning(f"读取记忆缓存失败，回退数据库：key={key}，原因={exc}")

            # 缓存未命中，执行原查询并缓存Redis
            result = await func(*args, **kwargs)

            # 缓存未命中，回填Redis
            if not cache.is_available:
                return result
            try:
                await cache.set_cache(key, serialize_cache(result), ttl_seconds)
                logger.debug(f"记忆缓存写入：{key}")
            except Exception as exc:
                cache.mark_unavailable()
                logger.warning(f"写入记忆缓存失败：key={key}，原因={exc}")
            return result

        return wrapper

    return decorator


def invalidate_cache(
    patterns_builder,
    *,
    should_invalidate=None,  # 缓存失效判断函数,用于L1的乐观锁
):
    """原更新数据库函数成功后删除相关缓存"""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # 执行原写入函数
            result = await func(*args, **kwargs)

            #
            if should_invalidate is not None and not should_invalidate(result):
                return result

            patterns = list(patterns_builder(result, *args, **kwargs))
            cache = get_redis_memory_cache()
            if not patterns:
                return result
            if not cache.is_available:
                if cache.is_configured:
                    cache.defer_cache_delete(patterns)
                return result

            try:
                await cache.delete_cache(patterns)
                logger.debug(f"记忆缓存已失效：{patterns}")
            except Exception as exc:
                cache.defer_cache_delete(patterns)
                cache.mark_unavailable()
                logger.warning(f"记忆缓存失效失败：patterns={patterns}，原因={exc}")
            return result

        return wrapper

    return decorator
