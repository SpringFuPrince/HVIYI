import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.mysql_utils import get_mysql_client, get_mysql_session
from app.db.memory_models import Meeting, MeetingDocument, utc_now
from app.graph.import_process.agent.main_graph import compiled_import_graph
from app.graph.import_process.agent.state import create_default_state
from app.utils.logger import logger
from app.utils.path_util import PROJECT_ROOT
from app.utils.task_utils import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    add_done_task,
    add_running_task,
    get_done_task_list,
    get_running_task_list,
    get_task_status,
    update_task_status,
)


router = APIRouter()
SUPPORTED_SUFFIXES = {".pdf", ".pptx", ".docx", ".md"}


async def run_import_graph(
    *,
    task_id: str,
    file_path: str,
    meeting_id: str,
    document_id: str,
    is_transcript: bool = False,
) -> None:
    """在一场会议范围内执行 Import Graph，并同步任务与文档状态。"""

    update_task_status(task_id, TASK_STATUS_PROCESSING)
    await _update_document_status(document_id, "processing")
    state = create_default_state(
        task_id=task_id,
        meeting_id=meeting_id,
        document_id=document_id,
        input_doc_path=str(Path(file_path).resolve()),
        output_local_dir=str(PROJECT_ROOT / "output"),
        is_transcript=is_transcript,
    )
    try:
        async for event in compiled_import_graph.astream(state, stream_mode="updates"):
            for node_name in event:
                logger.info(f"task_id={task_id} 导入节点完成: {node_name}")
        update_task_status(task_id, TASK_STATUS_COMPLETED)
        await _update_document_status(document_id, "completed")
        logger.info(f"task_id={task_id} 导入图执行完毕")
    except Exception as exc:
        update_task_status(task_id, TASK_STATUS_FAILED)
        await _update_document_status(document_id, "failed", str(exc))
        logger.exception(f"task_id={task_id} 导入图执行失败: {exc}")
        raise


async def _update_document_status(
    document_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """同步 Import Graph 状态到会议文档记录。"""

    async with get_mysql_client().transaction() as session:
        await session.execute(
            update(MeetingDocument)
            .where(MeetingDocument.document_id == document_id)
            .values(status=status, error=error, updated_at=utc_now())
        )


async def _run_background_import(**kwargs: Any) -> None:
    """后台运行 Import Graph，处理异常并更新任务状态。"""
    try:
        await run_import_graph(**kwargs)
    except Exception as exc:
        task_id = str(kwargs["task_id"])
        update_task_status(task_id, TASK_STATUS_FAILED)
        logger.exception(f"task_id={task_id} 后台导入失败: {exc}")


@router.post("/upload")
async def upload_files(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    meeting_id: str = Form(...),
    is_transcript: bool = Form(False),
    db: AsyncSession = Depends(get_mysql_session),
):
    """
    上传文档并执行import graph
    1. 校验meeting_id并查询数据库检查是否存在
    2. 每个文件生成一个task_id和document_id
    3. 校验文件类型是否支持
    4. 文件保存到临时目录
    5. 添加MeetingDocument记录
    6. 启动后台任务执行import graph
    """
    clean_meeting_id = meeting_id.strip()
    if not clean_meeting_id:
        raise HTTPException(status_code=422, detail="meeting_id不能为空")
    meeting = await db.scalar(
        select(Meeting).where(Meeting.meeting_id == clean_meeting_id)
    )
    if meeting is None:
        raise HTTPException(status_code=404, detail="会议不存在")
    if not files:
        raise HTTPException(status_code=400, detail="至少上传一个文件")

    temp_root = PROJECT_ROOT / "output" / "temp"
    task_ids: list[str] = []
    for upload in files:
        task_id = str(uuid.uuid4())
        document_id = f"doc_{uuid.uuid4().hex}"
        file_name = Path(upload.filename or "").name.strip()
        if not file_name or file_name in {".", ".."}:
            raise HTTPException(status_code=400, detail="上传文件缺少有效文件名")
        suffix = Path(file_name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            supported = ", ".join(sorted(item[1:] for item in SUPPORTED_SUFFIXES))
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型，仅支持：{supported}",
            )
        if is_transcript and suffix != ".md":
            raise HTTPException(status_code=400, detail="会议转录必须上传Markdown文件")

        task_dir = temp_root / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        saved_file_path = task_dir / file_name
        add_running_task(task_id, "upload_file")
        update_task_status(task_id, "pending")
        try:
            with saved_file_path.open("wb") as buffer:
                shutil.copyfileobj(upload.file, buffer)
        except OSError as exc:
            update_task_status(task_id, TASK_STATUS_FAILED)
            logger.exception(f"task_id={task_id} 保存上传文件失败: {saved_file_path}")
            raise HTTPException(status_code=500, detail="服务端保存上传文件失败") from exc
        finally:
            await upload.close()

        task_ids.append(task_id)
        db.add(
            MeetingDocument(
                document_id=document_id,
                meeting_id=meeting.meeting_id,
                file_name=file_name,
                source_type="transcript_md" if is_transcript else suffix[1:],
                status="pending",
                original_path=str(saved_file_path),
            )
        )
        add_done_task(task_id, "upload_file")
        background_tasks.add_task(
            _run_background_import,
            task_id=task_id,
            file_path=str(saved_file_path),
            meeting_id=meeting.meeting_id,
            document_id=document_id,
            is_transcript=is_transcript,
        )

    await db.commit()
    return {
        "code": 200,
        "message": f"完成文件上传，并开启异步导入任务，文件数量：{len(files)}",
        "task_ids": task_ids,
    }


@router.get("/status/{task_id}")
async def get_task_progress(task_id: str):
    status = get_task_status(task_id)
    if not status:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    return {
        "code": 200,
        "task_id": task_id,
        "status": status,
        "done_list": get_done_task_list(task_id),
        "running_list": get_running_task_list(task_id),
    }
