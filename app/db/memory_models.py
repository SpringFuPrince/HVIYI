from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utc_now() -> datetime:
    """MySQL DATETIME 按 UTC 保存无时区 datetime。"""

    return datetime.now(UTC).replace(tzinfo=None)


MYSQL_TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
}


class Meeting(Base):
    """本地版的根业务实体；Graph、文档和四层记忆都归属于一场会议。"""

    __tablename__ = "meetings"
    __table_args__ = (
        Index("idx_meeting_updated", "updated_at"),
        MYSQL_TABLE_OPTIONS,
    )

    meeting_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="scheduled", nullable=False)
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime)
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime)
    actual_start: Mapped[datetime | None] = mapped_column(DateTime)
    actual_end: Mapped[datetime | None] = mapped_column(DateTime)
    location: Mapped[str | None] = mapped_column(String(255))
    participant_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class MeetingDocument(Base):
    """Import Graph 的文档入口记录；正文和向量仍由文件存储与 Milvus 保存。"""

    __tablename__ = "meeting_documents"
    __table_args__ = (
        UniqueConstraint(
            "meeting_id",
            "document_id",
            name="uq_meeting_document_scope",
        ),
        Index(
            "idx_meeting_document_status",
            "meeting_id",
            "status",
            "updated_at",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("meetings.meeting_id", ondelete="CASCADE"),
        nullable=False,
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    original_path: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class RecordingSession(Base):
    """本地实时转写的一次录音会话。"""

    __tablename__ = "recording_sessions"
    __table_args__ = (
        Index("idx_recording_session_meeting", "meeting_id", "created_at"),
        MYSQL_TABLE_OPTIONS,
    )

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("meetings.meeting_id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), default="recording", nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    language: Mapped[str] = mapped_column(String(20), default="zh-CN", nullable=False)
    audio_dir: Mapped[str | None] = mapped_column(Text)
    transcript_path: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[str | None] = mapped_column(String(64))
    import_task_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class TranscriptSegment(Base):
    """ASR 的最终片段；sequence 保证前端重连后顺序可恢复。"""

    __tablename__ = "transcript_segments"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_transcript_segment_sequence"),
        Index("idx_transcript_segment_session_time", "session_id", "start_ms"),
        MYSQL_TABLE_OPTIONS,
    )

    segment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("recording_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker_name: Mapped[str] = mapped_column(String(100), default="本机用户", nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    confidence: Mapped[float | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class ChatMessage(Base):
    """L1 当前会议中某个会话的原始消息。"""

    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint(
            "meeting_id",
            "message_id",
            name="uq_chat_message_scope_message",
        ),
        UniqueConstraint(
            "meeting_id",
            "session_id",
            "turn_id",
            "role",
            name="uq_chat_message_turn_role",
        ),
        Index(
            "idx_chat_message_session_sequence",
            "meeting_id",
            "session_id",
            "sequence_id",
        ),
        Index(
            "idx_chat_message_meeting_time",
            "meeting_id",
            "created_at",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    # 数据库分配的稳定写入顺序，L1 滚动摘要用它作为游标。
    sequence_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    meeting_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("meetings.meeting_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)


class SessionMemoryState(Base):
    """L1 滚动摘要"""

    __tablename__ = "session_memory_states"
    __table_args__ = (
        UniqueConstraint(
            "meeting_id",
            "session_id",
            name="uq_session_memory_state_scope",
        ),
        Index(
            "idx_session_memory_state_scope",
            "meeting_id",
            "session_id",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    meeting_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("meetings.meeting_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summarized_through_sequence_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class OfficeTask(Base):
    """L4：从一场会议中提取并持续更新的办公任务。"""

    __tablename__ = "office_tasks"
    __table_args__ = (
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_office_task_progress"),
        Index(
            "idx_office_task_meeting_open",
            "meeting_id",
            "status",
            "due_at",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("meetings.meeting_id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="todo", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

