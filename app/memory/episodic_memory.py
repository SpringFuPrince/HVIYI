from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from app.conf.redis_config import redis_config
from app.memory.models import MeetingEpisodeContent,MemoryFactContent,MemoryKind,MemoryResponse
from app.memory.cache_utils import build_l2_episode_cache_key,build_l2_profile_cache_key,cache_aside,invalidate_cache


def _l2_episode_cache_key(_self,request) -> str:
    """
    会议情景记忆缓存key
    """
    return build_l2_episode_cache_key(request.scope.meeting_id)


def _l2_profile_cache_key(_self,_request) -> str:
    """
    用户画像缓存key
    """
    return build_l2_profile_cache_key()


def _l2_episode_invalidation(_result,_self,request,content) -> list[str]:
    """
    需要删除的会议情景记忆缓存key列表
    """
    return [  build_l2_episode_cache_key(request) ]


def _l2_profile_invalidation(_result,_self,request,content) -> list[str]:
    """
    需要删除的用户画像缓存key列表
    """
    return [build_l2_profile_cache_key()]



def utc_now() -> datetime:
    return datetime.now(UTC)

# L2 会议情景记忆与用户画像
class EpisodicMemory:

    def __init__(self, mongo_client):
        self._mongo = mongo_client

    async def write(self, request):
        if request.kind == MemoryKind.MEETING_EPISODE:
            content = MeetingEpisodeContent.model_validate(request.content)
            return await self.save_meeting(request, content)

        if request.kind == MemoryKind.MEMORY_FACT:
            content = MemoryFactContent.model_validate(request.content)
            return await self.save_profile_attributes(request, content)

        raise ValueError(f"L2不支持kind={request.kind.value}")

    @invalidate_cache(_l2_episode_invalidation)
    async def save_meeting(self,request,content: MeetingEpisodeContent) -> dict:
        """
        保存会议情景记忆
        """

        meeting_id = request.scope.meeting_id
        collection = self._mongo.meeting_episodes
        # 查看是否存在该会议情景记忆
        existing = await collection.find_one(
            {"meeting_id": meeting_id},
            {"last_event_id": 1, "revision": 1},
        )
        # 重复保存，返回失败
        if existing and existing.get("last_event_id") == request.event_id:
            return {
                "meeting_id": meeting_id,
                "updated": False,
                "revision": existing.get("revision", 1),
            }

        now = utc_now()
        result = await collection.update_one(
            {"meeting_id": meeting_id},
            {
                "$set": {
                    "meeting_data": content.model_dump(mode="json"),
                    "last_event_id": request.event_id,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "meeting_id": meeting_id,
                    "created_at": now,
                },
                "$inc": {"revision": 1},
            },
            upsert=True,
        )
        return {
            "meeting_id": meeting_id,
            "created": result.upserted_id is not None,
            "updated": result.modified_count > 0,
        }

    @invalidate_cache(_l2_profile_invalidation)
    async def save_profile_attributes(self,request,content: MemoryFactContent) -> dict[str, Any]:
        """
        遍历所有画像属性，拆成多个属性文档保存
        """

        if not content.profile:
            return {"updated": False, "attributes": []}

        results = []
        # 遍历每个画像属性，拆成多个属性文档
        for attribute_key, attribute_value in content.profile.items():
            results.append( await self._save_profile_attribute(request,attribute_key,attribute_value) )
        return {
            "updated": any(item["updated"] for item in results),
            "attributes": results,
        }

    async def _save_profile_attribute(
        self,
        request,
        attribute_key: str,
        attribute_value: Any,
    ) -> dict[str, Any]:
        """
        保存用户画像偏好
        """

        collection = self._mongo.profile_attributes
        attribute_filter = {"attribute_key": attribute_key}

        for _ in range(3):
            existing = await collection.find_one(attribute_filter)
            if existing and existing.get("last_event_id") == request.event_id:
                return {
                    "attribute_key": attribute_key,
                    "updated": False,
                    "version": existing.get("version", 1),
                }

            now = utc_now()
            next_version = int((existing or {}).get("version", 0)) + 1
            values = {
                "attribute_value": attribute_value,
                "last_event_id": request.event_id,
                "source_meeting_id": request.scope.meeting_id,
                "source_session_id": request.scope.session_id,
                "updated_at": now,
                "version": next_version,
            }

            if existing is None:
                try:
                    await collection.insert_one(
                        {
                            "attribute_key": attribute_key,
                            **values,
                            "created_at": now,
                        }
                    )
                    return {
                        "attribute_key": attribute_key,
                        "updated": True,
                        "version": next_version,
                    }
                except DuplicateKeyError:
                    continue

            result = await collection.update_one(
                {
                    "_id": existing["_id"],
                    "version": existing.get("version", 0),
                },
                {"$set": values},
            )
            if result.modified_count == 1:
                return {
                    "attribute_key": attribute_key,
                    "updated": True,
                    "version": next_version,
                }

        raise RuntimeError(f"画像属性并发更新冲突：{attribute_key}")

    async def recall(self,request) -> MemoryResponse:
        """
        分别召回：
        1. 当前会议的会议情节。
        2. 本机用户的全局画像。
        """
        meeting_document = (await self._recall_meeting(request))
        profile = await self._recall_profile(request)

        return MemoryResponse(
            meeting_episode=meeting_document,
            facts=profile,
        )

    @cache_aside(
        key_builder=_l2_episode_cache_key,
        ttl_seconds=redis_config.l2_ttl_seconds,
    )
    async def _recall_meeting(
        self,
        request,
    ) -> dict | None:
        meeting_document = (
            await self._mongo.meeting_episodes.find_one(
                {
                    "meeting_id": request.scope.meeting_id
                }
            )
        )

        if meeting_document:
            meeting_document["_id"] = str(meeting_document["_id"])
        return meeting_document

    @cache_aside(
        key_builder=_l2_profile_cache_key,
        ttl_seconds=redis_config.l2_ttl_seconds,
    )
    async def _recall_profile(
        self,
        _request,
    ) -> dict[str, Any]:
        cursor = self._mongo.profile_attributes.find(
            # 存在 attribute_key 字段的文档
            {
                "attribute_key": {
                    "$exists": True
                }
            },
            # 只返回 attribute_key 和 attribute_value 字段
            {
                "attribute_key": 1,
                "attribute_value": 1,
            },
        # 按 attribute_key 排序
        ).sort(
            "attribute_key",
            ASCENDING,
        )

        documents = await cursor.to_list(length=None)

        return {
            document["attribute_key"] : document.get("attribute_value") for document in documents
        }
