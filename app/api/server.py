"""本地单用户业务 API：会议、任务、文档和实时 ASR。"""

import json
import os
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.asr.engine import AsrResult, AsrUnavailableError, create_streaming_asr_session, merge_stream_text
from app.clients.mongo_utils import get_mongo_memory_client
from app.clients.mysql_utils import get_mysql_client, get_mysql_session
from app.db.memory_models import (
    Meeting,
    MeetingDocument,
    OfficeTask,
    RecordingSession,
    TranscriptSegment,
    utc_now,
)
from app.graph.import_process.api.import_service import run_import_graph
from app.memory.cache_utils import build_l4_task_cache_target, invalidate_cache
from app.utils.logger import logger
from app.utils.path_util import PROJECT_ROOT


MEETING_STATUSES = {"scheduled", "in_progress", "completed", "cancelled"}
DEFAULT_LOCAL_DISPLAY_NAME = "老己"
DISPLAY_NAME_PROFILE_KEYS = ("姓名", "name", "称呼")


router = APIRouter()


def _resolve_display_name(documents: list[dict[str, Any]]) -> str:
    """按固定优先级从画像属性文档中解析前端称呼。"""

    attributes = {
        str(document.get("attribute_key", "")).strip(): document.get("attribute_value")
        for document in documents
    }
    for key in DISPLAY_NAME_PROFILE_KEYS:
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return DEFAULT_LOCAL_DISPLAY_NAME


class MeetingCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)
    scheduled_start: datetime
    scheduled_end: datetime | None = None
    location: str | None = Field(default=None, max_length=255)
    participant_count: int = Field(default=1, ge=1, le=999)


class MeetingStatusRequest(BaseModel):
    status: Literal["scheduled", "in_progress", "completed", "cancelled"]


class TaskProgressRequest(BaseModel):
    progress: int = Field(ge=0, le=100)


class RecordingStartRequest(BaseModel):
    mime_type: str = Field(default="audio/pcm;rate=16000", max_length=100)
    language: str = Field(default="zh-CN", max_length=20)


def _meeting_payload(meeting: Meeting) -> dict:
    return {
        "meeting_id": meeting.meeting_id,
        "title": meeting.title,
        "description": meeting.description,
        "status": meeting.status,
        "scheduled_start": meeting.scheduled_start,
        "scheduled_end": meeting.scheduled_end,
        "actual_start": meeting.actual_start,
        "actual_end": meeting.actual_end,
        "location": meeting.location,
        "participant_count": meeting.participant_count,
        "created_at": meeting.created_at,
        "updated_at": meeting.updated_at,
    }


def _task_payload(task: OfficeTask) -> dict:
    return {
        "task_id": task.id,
        "meeting_id": task.meeting_id,
        "title": task.title,
        "description": task.description,
        "owner": task.owner,
        "status": task.status,
        "progress": task.progress,
        "due_at": task.due_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _local_task_progress_invalidation(
    result: dict,
    *_args,
    **_kwargs,
) -> list[str]:
    meeting_id = result.get("meeting_id")
    if not meeting_id:
        return []
    return build_l4_task_cache_target(meeting_id)


def _segment_payload(segment: TranscriptSegment) -> dict:
    return {
        "segment_id": segment.segment_id,
        "sequence": segment.sequence,
        "speaker_name": segment.speaker_name,
        "text": segment.text,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "confidence": segment.confidence,
    }


async def _require_meeting(db: AsyncSession, meeting_id: str) -> Meeting:
    meeting = await db.get(Meeting, meeting_id.strip())
    if meeting is None:
        raise HTTPException(status_code=404, detail="会议不存在")
    return meeting


@router.get("/health")
async def health() -> dict:
    return {"status": "healthy"}


@router.get("/profile")
async def get_local_profile() -> dict[str, str]:
    """读取本机用户画像；MongoDB不可用时返回稳定的前端默认称呼。"""

    try:
        cursor = get_mongo_memory_client().profile_attributes.find(
            {"attribute_key": {"$in": list(DISPLAY_NAME_PROFILE_KEYS)}},
            {"_id": 0, "attribute_key": 1, "attribute_value": 1},
        )
        documents = await cursor.to_list(length=len(DISPLAY_NAME_PROFILE_KEYS))
    except Exception as exc:
        logger.warning(f"读取本机用户画像失败，使用默认称呼：{exc}")
        documents = []
    return {"display_name": _resolve_display_name(documents)}


@router.post("/meetings")
async def create_meeting(
    request: MeetingCreateRequest,
    db: AsyncSession = Depends(get_mysql_session),
) -> dict:
    if request.scheduled_end and request.scheduled_end <= request.scheduled_start:
        raise HTTPException(status_code=422, detail="scheduled_end必须晚于scheduled_start")
    meeting = Meeting(
        meeting_id=f"mtg_{uuid.uuid4().hex}",
        title=request.title.strip(),
        description=request.description.strip() if request.description else None,
        scheduled_start=request.scheduled_start,
        scheduled_end=request.scheduled_end,
        location=request.location.strip() if request.location else None,
        participant_count=request.participant_count,
    )
    db.add(meeting)
    await db.commit()
    await db.refresh(meeting)
    return _meeting_payload(meeting)


@router.get("/meetings")
async def list_meetings(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    query: str = Query(default="", max_length=100),
    db: AsyncSession = Depends(get_mysql_session),
) -> dict:
    conditions = []
    if query.strip():
        conditions.append(Meeting.title.contains(query.strip()))
    total = int(await db.scalar(select(func.count()).select_from(Meeting).where(*conditions)) or 0)
    rows = list((await db.scalars(
        select(Meeting)
        .where(*conditions)
        .order_by(Meeting.scheduled_start.desc(), Meeting.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all())
    return {"items": [_meeting_payload(item) for item in rows], "page": page, "page_size": page_size, "total": total}


@router.get("/meetings/nearest")
async def nearest_meeting(db: AsyncSession = Depends(get_mysql_session)) -> dict | None:
    now = utc_now()
    for status, ordering in (
        ("in_progress", Meeting.actual_start.desc()),
        ("scheduled", Meeting.scheduled_start.asc()),
        ("completed", Meeting.actual_end.desc()),
    ):
        meeting = await db.scalar(select(Meeting).where(Meeting.status == status).order_by(ordering).limit(1))
        if meeting is not None:
            return _meeting_payload(meeting)
    return None


@router.get("/meetings/{meeting_id}")
async def get_meeting(meeting_id: str, db: AsyncSession = Depends(get_mysql_session)) -> dict:
    return _meeting_payload(await _require_meeting(db, meeting_id))


@router.patch("/meetings/{meeting_id}/status")
async def update_meeting_status(
    meeting_id: str,
    request: MeetingStatusRequest,
    db: AsyncSession = Depends(get_mysql_session),
) -> dict:
    meeting = await _require_meeting(db, meeting_id)
    now = utc_now()
    meeting.status = request.status
    if request.status == "in_progress" and meeting.actual_start is None:
        meeting.actual_start = now
    if request.status in {"completed", "cancelled"}:
        meeting.actual_end = now
    await db.commit()
    await db.refresh(meeting)
    return _meeting_payload(meeting)


@router.get("/meetings/{meeting_id}/documents")
async def list_meeting_documents(meeting_id: str, db: AsyncSession = Depends(get_mysql_session)) -> dict:
    await _require_meeting(db, meeting_id)
    rows = list((await db.scalars(
        select(MeetingDocument)
        .where(MeetingDocument.meeting_id == meeting_id)
        .order_by(MeetingDocument.created_at.desc())
    )).all())
    return {"items": [{
        "document_id": row.document_id,
        "meeting_id": row.meeting_id,
        "file_name": row.file_name,
        "source_type": row.source_type,
        "status": row.status,
        "error": row.error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    } for row in rows]}


@router.get("/tasks")
async def list_tasks(
    meeting_id: str | None = None,
    incomplete_only: bool = Query(default=True),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=6, ge=1, le=100),
    db: AsyncSession = Depends(get_mysql_session),
) -> dict:
    conditions = []
    if meeting_id:
        conditions.append(OfficeTask.meeting_id == meeting_id.strip())
    if incomplete_only:
        conditions.append(OfficeTask.status == "todo")
    total = int(await db.scalar(select(func.count()).select_from(OfficeTask).where(*conditions)) or 0)
    rows = list((await db.scalars(
        select(OfficeTask)
        .where(*conditions)
        .order_by(OfficeTask.due_at.asc(), OfficeTask.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all())
    return {"items": [_task_payload(row) for row in rows], "page": page, "page_size": page_size, "total": total}


@router.patch("/tasks/{task_id}/progress")
@invalidate_cache(_local_task_progress_invalidation)
async def update_task_progress(
    task_id: str,
    request: TaskProgressRequest,
    db: AsyncSession = Depends(get_mysql_session),
) -> dict:
    task = await db.get(OfficeTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="办公任务不存在")
    task.progress = request.progress
    task.status = "done" if request.progress == 100 else "todo"
    task.completed_at = utc_now() if request.progress == 100 else None
    await db.commit()
    await db.refresh(task)
    return _task_payload(task)


@router.post("/meetings/{meeting_id}/recordings/start")
async def start_recording(
    meeting_id: str,
    request: RecordingStartRequest,
    db: AsyncSession = Depends(get_mysql_session),
) -> dict:
    meeting = await _require_meeting(db, meeting_id)
    if meeting.status == "scheduled":
        meeting.status = "in_progress"
        meeting.actual_start = utc_now()
    session_id = f"rec_{uuid.uuid4().hex}"
    audio_dir = PROJECT_ROOT / "output" / "recordings" / meeting_id / session_id
    audio_dir.mkdir(parents=True, exist_ok=True)
    session = RecordingSession(
        session_id=session_id,
        meeting_id=meeting.meeting_id,
        mime_type=request.mime_type,
        language=request.language,
        audio_dir=str(audio_dir),
        started_at=utc_now(),
    )
    db.add(session)
    await db.commit()
    return {
        "session_id": session_id,
        "meeting_id": meeting_id,
        "status": "recording",
        "started_at": session.started_at,
        "last_sequence": -1,
        "websocket_url": f"/ws/recordings/{session_id}",
    }


@router.get("/recordings/{session_id}/segments")
async def get_recording_segments(session_id: str, db: AsyncSession = Depends(get_mysql_session)) -> list[dict]:
    session = await db.get(RecordingSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="录音会话不存在")
    rows = list((await db.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.session_id == session_id)
        .order_by(TranscriptSegment.sequence.asc())
    )).all())
    return [_segment_payload(item) for item in rows]


async def _persist_transcript_segment(
    session_id: str,
    sequence: int,
    result: AsrResult,
    segment_id: str | None = None,
) -> TranscriptSegment:
    async with get_mysql_client().transaction() as db:
        existing = await db.scalar(select(TranscriptSegment).where(
            TranscriptSegment.session_id == session_id,
            TranscriptSegment.sequence == sequence,
        ))
        if existing is not None:
            return existing
        segment = TranscriptSegment(
            segment_id=segment_id or f"seg_{uuid.uuid4().hex}",
            session_id=session_id,
            sequence=sequence,
            speaker_name=result.speaker_name,
            text=result.text,
            start_ms=result.start_ms,
            end_ms=result.end_ms,
            confidence=result.confidence,
        )
        db.add(segment)
        await db.flush()
        return segment


@router.websocket("/ws/recordings/{session_id}")
async def recording_socket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    wave_writer: wave.Wave_write | None = None
    try:
        async with get_mysql_client().session() as db:
            session = await db.get(RecordingSession, session_id)
            last_sequence = await db.scalar(
                select(func.max(TranscriptSegment.sequence)).where(
                    TranscriptSegment.session_id == session_id
                )
            )
        if session is None or session.status != "recording":
            await websocket.send_json({"type": "error", "code": "SESSION_NOT_RECORDING", "message": "录音会话不存在或已结束"})
            await websocket.close(code=1008)
            return

        sample_rate = 16_000
        asr_session = create_streaming_asr_session(sample_rate)
        await asr_session.warmup()
        next_sequence = int(last_sequence) + 1 if last_sequence is not None else 0
        audio_dir = Path(session.audio_dir or PROJECT_ROOT / "output" / "recordings" / session_id)
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_path = audio_dir / f"stream-{uuid.uuid4().hex}.wav"
        wave_writer = wave.open(str(audio_path), "wb")
        wave_writer.setnchannels(1)
        wave_writer.setsampwidth(2)
        wave_writer.setframerate(sample_rate)
        await websocket.send_json({
            "type": "ready",
            "session_id": session_id,
            "encoding": "pcm_s16le",
            "sample_rate": sample_rate,
        })

        live_segment_id: str | None = None
        live_text = ""
        live_start_ms = 0
        live_end_ms = 0
        silence_ms = 0.0
        silence_rms = float(os.getenv("ASR_SILENCE_RMS", "0.008"))
        endpoint_silence_ms = int(os.getenv("ASR_ENDPOINT_SILENCE_MS", "800"))
        sentence_split_threshold = int(os.getenv("ASR_SENTENCE_SPLIT_THRESHOLD", "80"))

        async def update_live_results(results: list[AsrResult]) -> None:
            nonlocal live_segment_id, live_text, live_start_ms, live_end_ms
            for result in results:
                if live_segment_id is None:
                    live_segment_id = f"seg_{uuid.uuid4().hex}"
                    live_start_ms = result.start_ms
                live_text = merge_stream_text(live_text, result.text)
                live_end_ms = max(live_end_ms, result.end_ms)
                if live_text:
                    await websocket.send_json({
                        "type": "partial",
                        "segment_id": live_segment_id,
                        "session_id": session_id,
                        "sequence": next_sequence,
                        "speaker_name": result.speaker_name,
                        "text": live_text,
                        "start_ms": live_start_ms,
                        "end_ms": live_end_ms,
                        "confidence": result.confidence,
                    })

        async def commit_live_result() -> None:
            nonlocal next_sequence, live_segment_id, live_text, live_start_ms, live_end_ms, silence_ms
            if live_segment_id is None or not live_text.strip():
                return
            final_text = await asr_session.punctuate(live_text)
            result = AsrResult(
                text=final_text,
                start_ms=live_start_ms,
                end_ms=live_end_ms,
            )
            segment = await _persist_transcript_segment(
                session_id,
                next_sequence,
                result,
                segment_id=live_segment_id,
            )
            await websocket.send_json({"type": "final", **_segment_payload(segment)})
            next_sequence += 1
            live_segment_id = None
            live_text = ""
            live_start_ms = 0
            live_end_ms = 0
            silence_ms = 0.0

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("text") is not None:
                payload = json.loads(message["text"])
                if payload.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                elif payload.get("type") == "finish":
                    await update_live_results(await asr_session.finish())
                    await commit_live_result()
                    await websocket.send_json({"type": "finished", "session_id": session_id})
                    break
                continue
            audio_bytes = message.get("bytes")
            if audio_bytes is None:
                continue
            wave_writer.writeframesraw(audio_bytes)
            samples = np.frombuffer(audio_bytes, dtype="<i2")
            chunk_ms = samples.size * 1000 / sample_rate
            rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32) / 32768.0)))) if samples.size else 0.0
            silence_ms = silence_ms + chunk_ms if rms < silence_rms else 0.0

            await update_live_results(await asr_session.push(audio_bytes))
            reached_endpoint = bool(live_text) and silence_ms >= endpoint_silence_ms
            # 长度只决定何时允许按句末标点切分，绝不按固定字符数硬截断。
            reached_sentence_end = (
                len(live_text) >= sentence_split_threshold
                and live_text.endswith(("。", "！", "？", "!", "?", "；", ";"))
            )
            if reached_endpoint:
                await update_live_results(await asr_session.finalize_utterance())
            if reached_endpoint or reached_sentence_end:
                await commit_live_result()
    except AsrUnavailableError as exc:
        await websocket.send_json({
            "type": "error",
            "code": "ASR_UNAVAILABLE",
            "message": str(exc),
        })
        await websocket.close(code=1011)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.exception(f"ASR WebSocket异常: {exc}")
        await websocket.send_json({"type": "error", "code": "ASR_SOCKET_ERROR", "message": "实时转写连接发生异常"})
    finally:
        if wave_writer is not None:
            wave_writer.close()


def _write_transcript_markdown(meeting: Meeting, session: RecordingSession, segments: list[TranscriptSegment]) -> Path:
    target_dir = PROJECT_ROOT / "output" / "transcripts" / meeting.meeting_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{session.session_id}.md"
    lines = [f"# {meeting.title} 转录", "", f"- 会议ID：{meeting.meeting_id}", f"- 录音会话：{session.session_id}", ""]
    for item in segments:
        start_seconds = item.start_ms // 1000
        timestamp = f"{start_seconds // 60:02d}:{start_seconds % 60:02d}"
        lines.append(f"[{timestamp}] {item.text}")
        lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


async def _run_transcript_import(**kwargs) -> None:
    try:
        await run_import_graph(**kwargs)
    except Exception as exc:
        logger.exception(f"转录导入失败: {exc}")


@router.post("/recordings/{session_id}/end")
async def end_recording(
    session_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_mysql_session),
) -> dict:
    session = await db.get(RecordingSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="录音会话不存在")
    if session.status in {"completed", "processing"}:
        return {
            "session_id": session.session_id,
            "meeting_id": session.meeting_id,
            "status": session.status,
            "transcript_generated": bool(session.transcript_path),
            "transcript_md_path": session.transcript_path,
            "import_task_id": session.import_task_id,
            "import_status": session.status,
        }
    meeting = await _require_meeting(db, session.meeting_id)
    segments = list((await db.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.session_id == session_id)
        .order_by(TranscriptSegment.sequence.asc())
    )).all())
    meeting.status = "completed"
    meeting.actual_end = utc_now()
    session.ended_at = utc_now()

    if not segments:
        session.status = "completed"
        await db.commit()
        return {
            "session_id": session.session_id,
            "meeting_id": session.meeting_id,
            "status": session.status,
            "transcript_generated": False,
            "transcript_md_path": None,
            "import_task_id": None,
            "import_status": "skipped",
        }

    transcript_path = _write_transcript_markdown(meeting, session, segments)
    document_id = f"doc_{uuid.uuid4().hex}"
    task_id = str(uuid.uuid4())
    db.add(MeetingDocument(
        document_id=document_id,
        meeting_id=meeting.meeting_id,
        file_name=transcript_path.name,
        source_type="transcript_md",
        status="pending",
        original_path=str(transcript_path),
    ))
    session.transcript_path = str(transcript_path)
    session.document_id = document_id
    session.import_task_id = task_id
    session.status = "processing"
    await db.commit()
    background_tasks.add_task(
        _run_transcript_import,
        task_id=task_id,
        file_path=str(transcript_path),
        meeting_id=meeting.meeting_id,
        document_id=document_id,
        is_transcript=True,
    )
    return {
        "session_id": session.session_id,
        "meeting_id": session.meeting_id,
        "status": "processing",
        "transcript_generated": True,
        "transcript_md_path": str(transcript_path),
        "import_task_id": task_id,
        "import_status": "pending",
    }


@router.get("/recordings/{session_id}/import-status")
async def recording_import_status(session_id: str, db: AsyncSession = Depends(get_mysql_session)) -> dict:
    session = await db.get(RecordingSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="录音会话不存在")
    if not session.document_id:
        return {"task_id": None, "status": "skipped"}
    document = await db.get(MeetingDocument, session.document_id)
    return {"task_id": session.import_task_id, "status": document.status if document else "failed"}
