from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from app.memory.models import (
    MeetingEpisodeContent,
    MemoryFactContent,
    MemoryKind,
    MemoryResponse,
)


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

    async def recall(self, request) -> MemoryResponse:
        """
        召回当前会议情节记忆，并聚合用户的全部画像属性。
        """

        meeting_document = await self._mongo.meeting_episodes.find_one(
            {"meeting_id": request.scope.meeting_id}
        )
        if meeting_document:
            meeting_document["_id"] = str(meeting_document["_id"])

        cursor = self._mongo.profile_attributes.find(
            {"attribute_key": {"$exists": True}},
            {"attribute_key": 1, "attribute_value": 1},
        ).sort("attribute_key", ASCENDING)
        documents = await cursor.to_list(length=None)
        profile = {
            document["attribute_key"]: document.get("attribute_value")
            for document in documents
        }

        return MemoryResponse(
            meeting_episode=meeting_document,
            facts=profile,
        )
