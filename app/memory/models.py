from enum import Enum
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

# 记忆分层枚举
class MemoryLayer(str, Enum):
    SHORT_TERM = "short_term"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    OFFICE_PROGRESS = "office_progress"

class MemoryKind(str, Enum):
    # L1
    CHAT_MESSAGE = "chat_message"
    # L2
    MEETING_EPISODE = "meeting_episode"  # 会议
    MEMORY_FACT = "memory_fact"  # 本机用户画像属性
    # L3
    CONVERSATION_TURN = "conversation_turn"
    # L4
    OFFICE_TASK_CREATED = "office_task_created"
    OFFICE_TASK_UPDATED = "office_task_updated"



class MemoryScope(BaseModel):
    meeting_id: str = Field(min_length=1, max_length=64, description="会议ID")
    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="会话ID；只有L1和对话写入需要",
    )

    @field_validator("meeting_id", "session_id")
    @classmethod
    def normalize_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("ID不能为空")
        return normalized


# 写入记忆的请求体
class MemoryWriteRequest(BaseModel):
    event_id: str = Field(...,description="本次记忆的唯一ID")
    layer: MemoryLayer = Field(...,description="记忆的层级")
    kind: MemoryKind = Field(...,description="当前层内部的具体记忆类型")
    scope: MemoryScope = Field(...,description="记忆的归属范围")
    content: dict[str, Any] = Field(...,description="记忆的具体内容")
    # source_refs: list[SourceReference] = Field(default_factory=list,description="记忆的来源引用")
    # confidence: float = Field(1.0,description="记忆的置信度")
    # importance: float = Field(0.5,description="记忆的重要度，可用于排序、淘汰或决定是否写入长期记忆")

# 回忆查询/召回的请求体
class MemoryRecallRequest(BaseModel):
    scope: MemoryScope = Field(description="记忆的归属范围")
    query: str = Field("",description="查询的文本")
    original_query: str = Field("", description="用户本轮原始问题；L4写操作以此为事实边界")
    turn_id: str | None = Field(default=None, description="当前对话轮次ID；用于生成稳定的L4任务ID")
    layers: set[MemoryLayer] = Field(default_factory=set,description="查询的层级")
    l3_top_k: int = Field(5, ge=1, le=50, description="L3语义召回的数量")
    l4_recent_limit: int = Field(10, ge=1, le=100, description="L4最近任务的默认召回数量")
    l4_top_k: int = Field(5, ge=1, le=50, description="L4语义召回的数量")


# 返回的Memory响应体
class MemoryResponse(BaseModel):
    # L1：摘要尚未覆盖的原始消息
    session_messages: list[dict] = Field(default_factory=list)
    session_summary: str = ""
    session_summary_version: int = 0                 #
    session_summarized_through_sequence_id: int = 0  # L1 摘要信息覆盖到的位置

    meeting_episode: dict | None = None  # L2 按meeting_id召回的一场会议情景信息
    facts: dict[str, Any] = Field(default_factory=dict)  # L2 用户的所有画像信息

    semantic_conversations: list[dict] = Field(default_factory=list)  # L3 从历史对话中语义检索到的片段
    office_progress: list[dict] = Field(default_factory=list)  # L4 需要长期跟踪的办公任务、当前进度、负责人和截止时间
    office_operation: dict[str, Any] | None = None  # L4 本轮创建、更新或召回的执行状态
    warnings: list[str] = Field(default_factory=list)    # 记录记忆查询过程中的非致命问题，如：未找到相关记忆、记忆过期等


# ===================各层记忆的content的schema==================


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

# L1
# 对话消息
class ChatMessageContent(StrictModel):
    message_id: str         # 消息的唯一ID
    turn_id: str            # 对话轮次的唯一ID
    role: str               # 消息的角色，例如：user、assistant、system
    content: str            # 消息的文本内容
    created_at: datetime    # 消息创建的时间


# L2
# import_graph中写入
# 每个会议只保存一个文档。summary是稳定字段，details允许大模型输出动态JSON。
class MeetingEpisodeContent(StrictModel):
    summary: str = Field(description="整场会议的总结")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="大模型生成的动态会议数据，例如任务、里程碑、决定和风险",
    )


# L2：从一轮对话中提取的一个或多个画像属性。
# MongoDB落库时会拆分成“一条文档一个属性”。
class MemoryFactContent(StrictModel):
    profile: dict[str, Any] = Field(
        default_factory=dict,
        description="本轮明确识别出的长期画像属性",
    )

    @field_validator("profile")
    @classmethod
    def normalize_profile(cls, value: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for raw_key, attribute_value in value.items():
            key = str(raw_key).strip()
            if not key:
                raise ValueError("画像属性名不能为空")
            if attribute_value is None:
                continue
            normalized[key] = attribute_value
        return normalized

# L3
# query_graph中写入
class ConversationTurnContent(StrictModel):
    user_message_id: str       # MySQL 中的用户消息ID
    assistant_message_id: str  # MySQL 中的助手最终回答消息ID
    conversation_time: datetime

#L4
# 创建办公任务
# import_graph 和 query_graph 都可写入
class OfficeTaskCreateContent(StrictModel):
    task_id: str             # 任务的唯一ID
    title: str               # 任务的标题

    description: str | None = None # 任务的详细描述
    owner: str | None = None     # 任务的关联人

    progress: int = Field(default=0, ge=0, le=100)  # 任务进度，0-100%
    status: Literal["todo", "done", "cancelled"] = "todo"      # 任务的状态：待办、完成、取消

    due_at: datetime | None = None # 任务的截止时间

# L4
# 更新办公任务进度
class OfficeTaskUpdateContent(StrictModel):
    task_id: str             # 任务的唯一ID
    progress: int = Field(ge=0, le=100) # 任务进度，0-100%
    status: Literal["todo", "done", "cancelled"]







