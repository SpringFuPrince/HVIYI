from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db.memory_models import ChatMessage, SessionMemoryState, utc_now
from app.memory.models import ChatMessageContent, MemoryKind, MemoryResponse



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
        召回会话的原始数据或滚动摘要。
        """

        scope = request.scope
        if not scope.session_id:
            return MemoryResponse()

        async with self._mysql.session() as session:
            state = await session.scalar(
                select(SessionMemoryState).where(
                    SessionMemoryState.meeting_id == scope.meeting_id,
                    SessionMemoryState.session_id == scope.session_id,
                )
            )
            summarized_through = state.summarized_through_sequence_id if state is not None   else 0
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

        return MemoryResponse(
            session_messages=[self._message_to_dict(row) for row in rows],
            session_summary=state.summary if state is not None else "",
            session_summary_version=state.version if state is not None else 0,
            session_summarized_through_sequence_id=summarized_through,
        )


    async def append_message(self, request, content):
        """
        写入会话消息
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
        供问题改写节点直接读取最近K条消息。
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
        """按写入顺序读取当前会话全部消息，供管理或导出api接口使用。"""

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

    async def save_session_summary(
        self,
        scope,
        summary: str,
        summarized_through_sequence_id: int,
        expected_version: int,
    ) -> bool:
        """用乐观锁保存摘要；版本已变化时返回 False，不覆盖并发结果。"""

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
