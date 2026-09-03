import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.clients.milvus_utils import (
    async_hybrid_search,
    create_hybrid_search_requests,
    get_async_milvus_client,
)
from app.conf.milvus_config import milvus_config
from app.db.memory_models import ChatMessage
from app.lm.embedding_utils import (
    generate_embeddings,
    generate_query_embeddings,
)
from app.memory.models import (
    ConversationTurnContent,
    MemoryKind,
    MemoryResponse,
)
from app.utils.escape_milvus_string_utils import escape_milvus_string


def _epoch_millis(value: datetime) -> int:
    """
    把对话时间统一转成UTC毫秒时间戳。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def _memory_id(request, content: ConversationTurnContent) -> str:
    """
    根据 meeting_id 和一对 message_id 生成稳定 memory_id。
    """

    raw = ":".join(
        [
            request.scope.meeting_id,
            content.user_message_id,
            content.assistant_message_id,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hit_entity(hit: dict[str, Any]) -> dict[str, Any]:
    """
    兼容 Milvus 搜索结果的两种返回格式
    获取命中文档
    """

    entity = hit.get("entity")
    return entity if isinstance(entity, dict) else hit

# L3 历史原始对话语义记忆
class SemanticMemory:
    def __init__(
        self,
        mysql_client,
        milvus_client=None,
        collection_name: str | None = None,
    ):
        self._mysql = mysql_client
        self._milvus = milvus_client or get_async_milvus_client()
        self._collection_name = (
            collection_name
            or milvus_config.conversation_memory_collection
        )

    async def write(self, request):
        """
        写入L3记忆
        先根据id从MySQL读取原文，然后写入
        """

        if request.kind != MemoryKind.CONVERSATION_TURN:
            raise ValueError(f"L3不支持kind={request.kind.value}")

        content = ConversationTurnContent.model_validate(request.content)
        user_message, assistant_message = await self.load_conversation_turn(request,content)
        search_text = (
            f"用户问题：{user_message.content}\n"
            f"ai回答：{assistant_message.content}"
        )
        embeddings = await asyncio.to_thread(generate_embeddings, [search_text])
        dense_vector = embeddings["dense"][0]
        sparse_vector = embeddings["sparse"][0]

        memory_id = _memory_id(request, content)
        await self._milvus.upsert(
            collection_name=self._collection_name,
            data=[
                {
                    "memory_id": memory_id,
                    "meeting_id": request.scope.meeting_id,
                    "session_id": user_message.session_id,
                    "user_message_id": user_message.message_id,
                    "assistant_message_id": assistant_message.message_id,
                    "conversation_time": _epoch_millis(content.conversation_time),
                    "status": "active",
                    "dense_vector": dense_vector,
                    "sparse_vector": sparse_vector,
                }
            ],
        )
        return {
            "memory_id": memory_id,
            "indexed": True,
        }

    async def load_conversation_turn(self,request,content: ConversationTurnContent) -> tuple[ChatMessage, ChatMessage]:
        """
        根据两个message_id从MySQL获取对话信息
        """
        message_ids = [
            content.user_message_id,
            content.assistant_message_id,
        ]
        async with self._mysql.session() as session:
            stmt = select(ChatMessage).where(
                ChatMessage.message_id.in_(message_ids),
                ChatMessage.meeting_id == request.scope.meeting_id,
            )
            result = await session.scalars(stmt)
            messages = list(result.all())

        by_id = {item.message_id: item for item in messages}
        user_message = by_id.get(content.user_message_id)
        assistant_message = by_id.get(content.assistant_message_id)
        if user_message is None:
            raise ValueError("找不到用户消息")
        if assistant_message is None:
            raise ValueError("找不到助手最终回答")
        if user_message.role != "user":
            raise ValueError("user_message_id不是用户消息")
        if assistant_message.role != "assistant":
            raise ValueError("assistant_message_id不是助手消息")
        if user_message.session_id != assistant_message.session_id:
            raise ValueError("两条消息不属于同一会话")
        if user_message.meeting_id != assistant_message.meeting_id:
            raise ValueError("两条消息不属于同一会议")
        if (
            request.scope.session_id
            and user_message.session_id != request.scope.session_id
        ):
            raise ValueError("消息不属于请求指定的session_id")
        if not user_message.content.strip() or not assistant_message.content.strip():
            raise ValueError("空问答不能写入语义记忆")
        return user_message, assistant_message

    async def recall(self, request) -> MemoryResponse:
        """
        向量检索历史问答
        """

        query = request.query.strip()
        if not query:
            return MemoryResponse()

        embeddings = await asyncio.to_thread(generate_query_embeddings,[query])
        filters = [
            f'meeting_id == "{escape_milvus_string(request.scope.meeting_id)}"',
            'status == "active"',
        ]

        filter_expression = " and ".join(filters)
        search_requests = create_hybrid_search_requests(
            dense_vector=embeddings["dense"][0],
            sparse_vector=embeddings["sparse"][0],
            dense_params={
                "metric_type": "COSINE",
                "params": {"ef": 64},
            },
            sparse_params={
                "metric_type": "IP",
                "params": {"drop_ratio_search": 0.2},
            },
            expr=filter_expression,
            limit=request.top_k,
        )
        search_result = await async_hybrid_search(
            client=self._milvus,
            collection_name=self._collection_name,
            reqs=search_requests,
            ranker_weights=(0.5, 0.5),
            norm_score=True,
            limit=request.top_k,
            output_fields=[
                "user_message_id",
                "assistant_message_id",
                "session_id",
                "conversation_time",
            ],
        )


        hits = search_result[0] if search_result else []
        message_ids: list[str] = []
        for hit in hits:
            entity = _hit_entity(hit)
            message_ids.extend(
                [
                    entity.get("user_message_id"),
                    entity.get("assistant_message_id"),
                ]
            )
        message_ids = list(dict.fromkeys(item for item in message_ids if item))
        if not message_ids:
            return MemoryResponse()

        async with self._mysql.session() as session:
            stmt = select(ChatMessage).where(
                ChatMessage.message_id.in_(message_ids),
                ChatMessage.meeting_id == request.scope.meeting_id,
            )
            result = await session.scalars(stmt)
            messages = {
                item.message_id: item
                for item in result.all()
            }

        conversations = []
        for hit in hits:
            entity = _hit_entity(hit)
            user_message = messages.get(entity.get("user_message_id"))
            assistant_message = messages.get(
                entity.get("assistant_message_id")
            )
            if user_message is None or assistant_message is None:
                continue
            conversations.append(
                {
                    "memory_id": hit.get("id") or entity.get("memory_id"),
                    "score": hit.get("distance"),
                    "session_id": entity.get("session_id"),
                    "conversation_time": entity.get("conversation_time"),
                    "user_message": {
                        "message_id": user_message.message_id,
                        "content": user_message.content,
                    },
                    "assistant_message": {
                        "message_id": assistant_message.message_id,
                        "content": assistant_message.content,
                    },
                }
            )

        return MemoryResponse(
            semantic_conversations=conversations
        )
