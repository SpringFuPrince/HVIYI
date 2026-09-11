import sys
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from app.graph.query_graph.agent.state import QueryGraphState
from app.lm.llm_utils import get_llm_client
from app.memory.memory_manager import get_memory_manager
from app.memory.models import MemoryKind, MemoryLayer, MemoryScope, MemoryWriteRequest
from app.utils.load_prompt import load_prompt
from app.utils.logger import logger
from app.utils.sse_utils import SSEEvent, push_to_session
from app.utils.task_utils import add_done_task, add_running_task


class UserPortraitResult(BaseModel):
    have_profile: bool = Field(description="是否识别到长期画像属性")
    profile: dict[str, Any] = Field(
        default_factory=dict,
        description="本轮明确发现的长期画像属性，没有则为空字典",
    )


def required_state_value(state, field_name) -> str:
    value = state.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"State 缺少有效字段：{field_name}")
    return value.strip()


def get_memory_scope(state: QueryGraphState) -> MemoryScope:
    """根据State中的业务ID构造记忆范围。"""
    return MemoryScope(
        meeting_id=required_state_value(state, "meeting_id"),
        session_id=required_state_value(state, "session_id"),
    )


def get_memory_event_id(state: QueryGraphState,content: dict[str, Any],kind: MemoryKind) -> str:
    """
    为不同记忆生成稳定、可重试的事件ID。
    """
    if kind == MemoryKind.CHAT_MESSAGE:
        return f"chat-message:{content['message_id']}"
    return f"{kind.value}:{required_state_value(state, 'assistant_message_id')}"


async def write_memory(state: QueryGraphState,content: dict[str, Any],layer: MemoryLayer,kind: MemoryKind) -> None:
    """
    复用的MemoryWriteRequest
    """
    event_id = get_memory_event_id(state, content, kind)
    request = MemoryWriteRequest(
        event_id=event_id,
        layer=layer,
        kind=kind,
        scope=get_memory_scope(state),
        content=content,
    )
    await get_memory_manager().write(request)


def utc_now() -> datetime:
    """
    MySQL DATETIME使用无时区UTC时间。
    """
    return datetime.now(UTC).replace(tzinfo=None)


async def save_user_message(state: QueryGraphState) -> None:
    """
    保存L1用户原始消息
    """
    content = {
        "message_id": required_state_value(state, "user_message_id"),
        "turn_id": required_state_value(state, "turn_id"),
        "role": "user",
        "content": required_state_value(state, "original_query"),
        "created_at": utc_now(),
    }
    await write_memory(
        state,
        content,
        MemoryLayer.SHORT_TERM,
        MemoryKind.CHAT_MESSAGE,
    )
    logger.info("成功保存L1用户消息")


async def save_assistant_message(state: QueryGraphState) -> None:
    """
    保存L1助手原始消息
    """
    content = {
        "message_id": required_state_value(state, "assistant_message_id"),
        "turn_id": required_state_value(state, "turn_id"),
        "role": "assistant",
        "content": required_state_value(state, "answer"),
        "created_at": utc_now(),
    }
    await write_memory(
        state,
        content,
        MemoryLayer.SHORT_TERM,
        MemoryKind.CHAT_MESSAGE,
    )
    logger.info("成功保存L1助手消息")


async def save_semantic_memory(state: QueryGraphState) -> None:
    """
    保存L3完整对话记忆
    """
    content = {
        "user_message_id": required_state_value(state, "user_message_id"),
        "assistant_message_id": required_state_value(state, "assistant_message_id"),
        "conversation_time": utc_now(),
    }
    await write_memory(
        state,
        content,
        MemoryLayer.SEMANTIC,
        MemoryKind.CONVERSATION_TURN,
    )
    logger.info("成功保存L3语义记忆")


async def recognition_user_portrait(state: QueryGraphState) -> dict[str, Any]:
    """
    从用户原始消息中识别用户的长期画像属性
    """

    prompt = load_prompt(
        "recognition_user_portrait",
        original_query=required_state_value(state, "original_query"),
    )
    structured_llm = get_llm_client().with_structured_output(
        UserPortraitResult,
        method="json_mode",
    )
    result = await structured_llm.ainvoke([HumanMessage(content=prompt)])
    if result.have_profile and result.profile:
        return result.profile
    return {}


async def save_user_portrait(state: QueryGraphState) -> None:
    """
    保存L2用户画像
    从用户原始消息中识别用户的长期画像属性，保存到L2记忆层
    """

    profile = await recognition_user_portrait(state)
    if not profile:
        return
    await write_memory(
        state,
        {"profile": profile},
        MemoryLayer.EPISODIC,
        MemoryKind.MEMORY_FACT,
    )
    logger.info(f"保存本机用户画像属性: {list(profile)}")

async def node_save_memory(state: QueryGraphState) -> QueryGraphState:
    """
    保存记忆
    L1始终保存
    L2、L3根据QueryPlan选择性执行
    """
    node_name = sys._getframe().f_code.co_name
    task_id = state["task_id"]
    is_stream = state.get("is_stream", False)
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(task_id, node_name, is_stream)

    memory_warnings: list[str] = []
    try:
        query_plan = state.get("query_plan")
        if not isinstance(query_plan, dict):
            raise ValueError("State缺少有效的query_plan")
        # 固定保存L1用户消息和助手消息
        await save_user_message(state)
        await save_assistant_message(state)

        # L2更新用户画像
        if query_plan.get("write_l2_portrait", False):
            try:
                await save_user_portrait(state)
            except Exception as exc:
                logger.exception(f"保存L2本机用户画像失败: {exc}")
                memory_warnings.append("L2本机用户画像保存失败")

        # L3保存完整问答索引
        if query_plan.get("write_l3_conversation", False):
            try:
                await save_semantic_memory(state)
            except Exception as exc:
                logger.exception(f"保存L3语义记忆失败: {exc}")
                memory_warnings.append("L3语义记忆保存失败")

        state["memory_warnings"] = memory_warnings
        add_done_task(task_id, node_name, is_stream)


        # 通过 SSE 给前端发送一个 最终完成事件
        if is_stream:
            push_to_session(
                task_id,
                SSEEvent.FINAL,
                {
                    "message_id": state["assistant_message_id"],
                    "answer": state["answer"],
                    "status": "completed",
                    "memory_warnings": memory_warnings,
                },
            )

        return state

    except Exception as exc:
        logger.exception(f"[{node_name}] 保存记忆失败: {exc}")
        raise
    finally:
        logger.info(f">>> [{node_name}]节点执行结束")
