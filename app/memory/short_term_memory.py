from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.db.memory_models import ChatMessage, SessionMemoryState, utc_now
from app.memory.models import ChatMessageContent, MemoryKind, MemoryResponse
from app.conf.redis_config import redis_config
from app.memory.cache_utils import cache_aside,invalidate_cache,build_l1_summary_cache_key




def _l1_summary_cache_key(_self, scope) -> str | None:
    """
    生成L1滚动摘要缓存key
    """
    if not scope.session_id:
        return None
    return build_l1_summary_cache_key( scope.meeting_id, scope.session_id )


def _l1_summary_invalidation(
    _result,
    _self,
    scope,
    summary: str,
    summarized_through_sequence_id: int,
    expected_version: int,
) -> list[str]:
    """
    获取需要删除的L1滚动摘要缓存key
    """
    if not scope.session_id:
        return []

    return [build_l1_summary_cache_key(scope.meeting_id, scope.session_id)]


# L1 短期记忆层，存储会话的原始数据和用于上下文压缩的滚动摘要
class ShortTermMemory:
    def __init__(self, mysql_client):
        self._mysql = mysql_client

    async def write(self, request):
        if request.kind == MemoryKind.CHAT_MESSAGE:
            content = ChatMessageContent.model_validate(request.content)
            return await self.append_message(request, content)

        raise ValueError(f"L1不支持kind={request.kind.value}")

    async def recall(self, request):
        """
        召回当前会话原始消息和滚动摘要
        """

        scope = request.scope
        if not scope.session_id:
            return MemoryResponse()
        # 查询滚动摘要
        summary_state = await self.recall_summary(scope)
        summarized_through = summary_state["summarized_through_sequence_id"]

        # 查询所有未被摘要的消息
        session_messages = await self.recall_unsummarized_messages(scope,summarized_through)

        return MemoryResponse(
            session_messages=session_messages,
            session_summary=summary_state["summary"],
            session_summary_version=summary_state["version"],
            session_summarized_through_sequence_id=summarized_through,
        )
    async def recall_unsummarized_messages(self, scope, summarized_through: int = 0) -> list[dict]:
        """
        查询未被摘要的原始消息
        """
        async with self._mysql.session() as session:
            result = await session.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.meeting_id == scope.meeting_id,
                    ChatMessage.session_id == scope.session_id,
                    ChatMessage.sequence_id > summarized_through,
                )
                .order_by(ChatMessage.sequence_id.asc())
            )
            rows = list(result.all())
        session_messages = [self._message_to_dict(row) for row in rows]
        return session_messages


    @cache_aside(
        key_builder=_l1_summary_cache_key,
        ttl_seconds=redis_config.l1_ttl_seconds,
    )
    async def recall_summary(self, scope) -> dict:
        """
        查询并缓存当前会话的滚动摘要
        """

        async with self._mysql.session() as session:
            state = await session.scalar(
                select(SessionMemoryState).where(
                    SessionMemoryState.meeting_id == scope.meeting_id,
                    SessionMemoryState.session_id == scope.session_id,
                )
            )

        if state is None:
            return {
                "summary": "",
                "version": 0,
                "summarized_through_sequence_id": 0,
            }

        return {
            "summary": state.summary,
            "version": state.version,
            "summarized_through_sequence_id": state.summarized_through_sequence_id,
        }

    async def append_message(self, request, content):
        """
        写入原始会话消息
        """
        scope = request.scope

        if not scope.session_id:
            raise ValueError("L1写入消息必须提供session_id")

        async with self._mysql.transaction() as session:
            stmt = select(ChatMessage.sequence_id).where(
                ChatMessage.message_id == content.message_id,
                ChatMessage.meeting_id == scope.meeting_id,
            )
            existing_sequence_id = await session.scalar(stmt)
            # 如果消息已存在，直接返回保存失败
            if existing_sequence_id is not None:
                return {
                    "message_id": content.message_id,
                    "sequence_id": existing_sequence_id,
                    "created": False,
                }

            message = ChatMessage(
                message_id=content.message_id,
                meeting_id=scope.meeting_id,
                session_id=scope.session_id,
                turn_id=content.turn_id,
                role=content.role,
                content=content.content,
                created_at=content.created_at,
            )

            session.add(message)
            await session.flush()

            return {
                "message_id": message.message_id,
                "sequence_id": message.sequence_id,
                "created": True,
            }

    async def get_recent_messages(self, scope, limit):
        """
        供问题改写节点直接读取最近K条原始消息
        """
        if not scope.session_id:
            return []

        async with self._mysql.session() as session:
            stmt = (
                select(ChatMessage)
                .where(
                    ChatMessage.meeting_id == scope.meeting_id,
                    ChatMessage.session_id == scope.session_id,
                )
                .order_by(ChatMessage.sequence_id.desc())
                .limit(limit)
            )
            result = await session.scalars(stmt)
            rows = list(result.all())

        # 倒序
        rows.reverse()

        return [self._message_to_dict(row) for row in rows]

    async def get_all_messages(self, scope):
        """
        按写入顺序读取当前会话全部原始消息，供管理或导出api接口使用
        """

        if not scope.session_id:
            return []

        async with self._mysql.session() as session:
            stmt = (
                select(ChatMessage)
                .where(
                    ChatMessage.meeting_id == scope.meeting_id,
                    ChatMessage.session_id == scope.session_id,
                )
                .order_by(ChatMessage.sequence_id.asc())
            )
            result = await session.scalars(stmt)
            rows = list(result.all())

        return [self._message_to_dict(row) for row in rows]

    @invalidate_cache(
        _l1_summary_invalidation,
        should_invalidate=lambda saved: saved is True, # 删除缓存的条件,返回True时删除缓存
    )
    async def save_session_summary(
        self,
        scope,
        summary: str,
        summarized_through_sequence_id: int,
        expected_version: int,
    ) -> bool:
        """
        用乐观锁保存原始会话滚动摘要
        """

        if not scope.session_id:
            raise ValueError("保存L1滚动摘要必须提供session_id")

        try:
            async with self._mysql.transaction() as session:
                result = await session.execute(
                    update(SessionMemoryState)
                    .where(
                        SessionMemoryState.meeting_id == scope.meeting_id,
                        SessionMemoryState.session_id == scope.session_id,
                        SessionMemoryState.version == expected_version,
                        SessionMemoryState.summarized_through_sequence_id
                        <= summarized_through_sequence_id,
                    )
                    .values(
                        summary=summary,
                        summarized_through_sequence_id=(
                            summarized_through_sequence_id
                        ),
                        version=SessionMemoryState.version + 1,
                        updated_at=utc_now(),
                    )
                )
                if result.rowcount == 1:
                    return True

                # 非首次写入更新不到一行，说明 version 已被其他请求推进。
                if expected_version != 0:
                    return False

                # version=0 且没有状态行时，创建会话的第一版摘要。
                session.add(
                    SessionMemoryState(
                        meeting_id=scope.meeting_id,
                        session_id=scope.session_id,
                        summary=summary,
                        summarized_through_sequence_id=(
                            summarized_through_sequence_id
                        ),
                        version=1,
                    )
                )
                await session.flush()
                return True
        except IntegrityError:
            # 两个请求都首次创建摘要时，唯一约束只允许其中一个成功。
            return False

    @staticmethod
    def _message_to_dict(row: ChatMessage) -> dict:
        return {
            "sequence_id": row.sequence_id,
            "message_id": row.message_id,
            "turn_id": row.turn_id,
            "role": row.role,
            "content": row.content,
            "created_at": row.created_at,
        }
