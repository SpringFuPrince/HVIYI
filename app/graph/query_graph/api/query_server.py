import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from app.clients.mysql_utils import get_mysql_session
from app.db.memory_models import ChatMessage, Meeting
from app.graph.query_graph.agent.main_graph import query_graph
from app.graph.query_graph.agent.state import create_query_default_state
from app.utils.logger import logger
from app.utils.sse_utils import (
    SSEEvent,
    create_sse_queue,
    get_sse_queue,
    push_to_session,
    sse_generator,
)
from app.utils.task_utils import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    get_done_task_list,
    get_running_task_list,
    get_task_status,
    set_task_result,
    update_task_status,
)


router = APIRouter()


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, description="用户原始问题")
    meeting_id: str = Field(
        min_length=1,
        max_length=64,
        description="必填；本次查询与四层记忆所属的会议ID",
    )
    session_id: str | None = Field(
        default=None,
        description="会话ID；为空时创建新会话",
    )
    is_stream: bool = Field(default=False, description="是否流式返回")


def _compact_title(content: str, max_length: int = 42) -> str:
    title = " ".join(content.split())
    if len(title) <= max_length:
        return title
    return f"{title[:max_length]}…"


async def _validate_continuing_session(
    db: AsyncSession,
    meeting_id: str,
    session_id: str | None,
) -> None:
    """已有会话只能在原会议中续聊，避免复用ID造成跨会议上下文串联。"""
    if session_id is None:
        return
    clean_session_id = session_id.strip()
    if not clean_session_id:
        raise HTTPException(status_code=422, detail="session_id不能为空")

    row = (
        await db.execute(
            select(ChatMessage.meeting_id)
            .where(
                ChatMessage.session_id == clean_session_id,
            )
            .limit(1)
        )
    ).first()
    if row is not None and row.meeting_id != meeting_id:
        raise HTTPException(status_code=409, detail="该会话属于另一场会议，请新建对话")


async def _require_meeting(
    db: AsyncSession,
    meeting_id: str,
) -> str:
    clean_meeting_id = meeting_id.strip()
    if not clean_meeting_id:
        raise HTTPException(status_code=422, detail="meeting_id不能为空")
    meeting = await db.scalar(
        select(Meeting).where(Meeting.meeting_id == clean_meeting_id)
    )
    if meeting is None:
        raise HTTPException(status_code=404, detail="会议不存在")
    return meeting.meeting_id


def _new_id() -> str:
    return str(uuid.uuid4())


def _create_query_state(
    request: QueryRequest,
    meeting_id: str,
):
    """一次用户询问对应一个task、turn和一对消息ID。"""
    return create_query_default_state(
        task_id=_new_id(),
        meeting_id=meeting_id,
        session_id=(request.session_id or _new_id()).strip(),
        turn_id=_new_id(),
        user_message_id=_new_id(),
        assistant_message_id=_new_id(),
        original_query=request.query.strip(),
        is_stream=request.is_stream,
        reranked_docs=[],
    )


async def run_query_graph(state, suppress_exception: bool = False):
    """异步执行Query Graph，并统一维护任务状态和SSE结束信号。"""
    task_id = state["task_id"]
    is_stream = state.get("is_stream", False)

    update_task_status(task_id, TASK_STATUS_PROCESSING, is_stream)
    try:
        result = await query_graph.ainvoke(state)
        # 回答节点已经发送FINAL；这里只更新状态，避免FINAL之后再出现progress事件。
        update_task_status(task_id, TASK_STATUS_COMPLETED, False)
        return result
    except Exception as exc:
        logger.exception(f"task_id={task_id} 查询图执行失败: {exc}")
        set_task_result(task_id, "error", "查询失败，请稍后重试。")
        update_task_status(task_id, TASK_STATUS_FAILED, is_stream)

        if is_stream:
            push_to_session(
                task_id,
                SSEEvent.ERROR,
                {"task_id": task_id, "message": "查询失败，请稍后重试。"},
            )

        if suppress_exception:
            return None
        raise
    finally:
        if is_stream:
            # FINAL或ERROR之后关闭本次task对应的SSE生成器。
            push_to_session(task_id, SSEEvent.CLOSE, {})


@router.get("/health")
async def health_status():
    return {"status": "healthy"}


@router.post("/query")
async def query(
    request: QueryRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_mysql_session),
):
    meeting_id = await _require_meeting(db, request.meeting_id)
    await _validate_continuing_session(
        db,
        meeting_id,
        request.session_id,
    )
    state = _create_query_state(request, meeting_id)
    task_id = state["task_id"]
    session_id = state["session_id"]

    if request.is_stream:
        # 流式队列按task_id创建，避免同一session的并发询问互相串流。
        create_sse_queue(task_id)
        background_tasks.add_task(run_query_graph, state, True)
        return {
            "message": "查询任务已创建",
            "task_id": task_id,
            "session_id": session_id,
            "stream_url": f"/stream/{task_id}",
        }

    try:
        result = await run_query_graph(state)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="查询失败，请稍后重试。") from exc

    return {
        "message": "本次处理完成",
        "task_id": task_id,
        "session_id": session_id,
        "answer": result.get("answer", ""),
        "memory_warnings": result.get("memory_warnings", []),
        "done_list": get_done_task_list(task_id),
    }


@router.get("/stream/{task_id}")
async def stream(task_id: str, request: Request):
    if get_sse_queue(task_id) is None:
        raise HTTPException(status_code=404, detail="查询任务不存在或流已经结束")

    return StreamingResponse(
        sse_generator(task_id, request),
        media_type="text/event-stream",
    )


@router.get("/tasks/{task_id}")
async def task_status(task_id: str):
    status = get_task_status(task_id)
    if not status:
        raise HTTPException(status_code=404, detail="查询任务不存在")

    return {
        "task_id": task_id,
        "status": status,
        "done_list": get_done_task_list(task_id),
        "running_list": get_running_task_list(task_id),
    }


@router.get("/history")
async def list_history(
    meeting_id: str = Query(min_length=1, max_length=64),
    search: str = Query(default="", max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_mysql_session),
):
    """分页读取指定会议中的真实 MySQL 会话。"""
    scope_meeting_id = await _require_meeting(db, meeting_id)
    conditions = [ChatMessage.meeting_id == scope_meeting_id]

    clean_search = search.strip()
    if clean_search:
        matching_sessions = (
            select(ChatMessage.session_id)
            .where(*conditions, ChatMessage.content.contains(clean_search))
            .distinct()
        )
        conditions.append(ChatMessage.session_id.in_(matching_sessions))

    grouped = (
        select(
            ChatMessage.session_id.label("session_id"),
            func.min(ChatMessage.created_at).label("created_at"),
            func.max(ChatMessage.created_at).label("updated_at"),
            func.count(ChatMessage.sequence_id).label("message_count"),
        )
        .where(*conditions)
        .group_by(ChatMessage.session_id)
    )
    total = int(
        await db.scalar(select(func.count()).select_from(grouped.subquery())) or 0
    )
    rows = (
        await db.execute(
            grouped.order_by(func.max(ChatMessage.sequence_id).desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    session_ids = [row.session_id for row in rows]
    titles: dict[str, str] = {}
    if session_ids:
        first_user_sequences = (
            select(
                ChatMessage.session_id.label("session_id"),
                func.min(ChatMessage.sequence_id).label("sequence_id"),
            )
            .where(
                ChatMessage.meeting_id == scope_meeting_id,
                ChatMessage.session_id.in_(session_ids),
                ChatMessage.role == "user",
            )
            .group_by(ChatMessage.session_id)
            .subquery()
        )
        first_user = aliased(ChatMessage)
        title_rows = (
            await db.execute(
                select(first_user.session_id, first_user.content).join(
                    first_user_sequences,
                    first_user.sequence_id == first_user_sequences.c.sequence_id,
                )
            )
        ).all()
        titles = {
            row.session_id: _compact_title(row.content)
            for row in title_rows
        }

    return {
        "items": [
            {
                "session_id": row.session_id,
                "meeting_id": scope_meeting_id,
                "title": titles.get(row.session_id, "未命名对话"),
                "message_count": row.message_count,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.get("/history/{session_id}")
async def history(
    session_id: str,
    meeting_id: str = Query(min_length=1, max_length=64),
    db: AsyncSession = Depends(get_mysql_session),
):
    """按稳定写入顺序返回一个会话的全部消息。"""
    scope_meeting_id = await _require_meeting(db, meeting_id)
    messages = list(
        (
            await db.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.meeting_id == scope_meeting_id,
                    ChatMessage.session_id == session_id.strip(),
                )
                .order_by(ChatMessage.sequence_id.asc())
            )
        ).all()
    )
    if not messages:
        raise HTTPException(status_code=404, detail="会话不存在")

    return {
        "session_id": session_id,
        "meeting_id": scope_meeting_id,
        "items": [
            {
                "sequence_id": message.sequence_id,
                "message_id": message.message_id,
                "turn_id": message.turn_id,
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at,
            }
            for message in messages
        ],
    }


@router.delete("/history/{session_id}")
async def delete_history(
    session_id: str,
):
    raise HTTPException(
        status_code=501,
        detail=f"尚未实现会话历史删除: {session_id}",
    )
