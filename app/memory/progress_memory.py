"""L4：按 meeting_id 保存办公任务的当前进度。"""
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db.memory_models import OfficeTask, utc_now
from app.conf.redis_config import redis_config
from app.memory.models import MemoryKind,MemoryResponse,OfficeTaskCreateContent,OfficeTaskUpdateContent
from app.memory.cache_utils import build_l4_progress_cache_key,build_l4_task_cache_target,cache_aside,invalidate_cache



def _l4_progress_cache_key(_self,request) -> str:
    """
    构建L4缓存key
    """
    return build_l4_progress_cache_key(request.scope.meeting_id,request.recent_limit)


def _l4_progress_invalidation(
    _result,
    _self,
    request,
    content,
) -> list[str]:
    """
    获取要删除的L4缓存key列表
    """
    return build_l4_task_cache_target(request.scope.meeting_id)


def _mysql_datetime(value: datetime | None) -> datetime | None:
    """把有时区时间统一转成MySQL DATETIME 无时区UTC的时间。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _task_dict(task: OfficeTask) -> dict[str, Any]:
    """ORM对象转成MemoryResponse可以直接返回的字典。"""

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
        "completed_at": task.completed_at,
    }


class OfficeProgressMemory:
    def __init__(self, mysql_client):
        self._mysql = mysql_client

    async def write(self, request):

        # 创建任务
        if request.kind == MemoryKind.OFFICE_TASK_CREATED:
            content = OfficeTaskCreateContent.model_validate(request.content)
            return await self.create_task(request, content)

        # 更新任务进度
        if request.kind == MemoryKind.OFFICE_TASK_UPDATED:
            content = OfficeTaskUpdateContent.model_validate(request.content)
            return await self.update_task(request, content)

        raise ValueError(f"L4不支持kind={request.kind.value}")

    @invalidate_cache(_l4_progress_invalidation)
    async def create_task(self,request,content: OfficeTaskCreateContent) -> dict[str, Any]:
        """
        使用调用方生成的稳定 task_id 幂等创建办公任务。
        """

        scope = request.scope

        try:
            async with self._mysql.transaction() as session:
                existing = await session.get(OfficeTask, content.task_id)
                if existing is not None:
                    if existing.meeting_id != scope.meeting_id:
                        raise ValueError(f"任务ID已经存在：{content.task_id}")
                    return {
                        "task_id": existing.id,
                        "created": False,
                    }

                completed_at = None
                if content.status == "done" or content.progress == 100:
                    completed_at = utc_now()

                task = OfficeTask(
                    id=content.task_id,
                    meeting_id=scope.meeting_id,
                    title=content.title,
                    description=content.description,
                    owner=content.owner,
                    status=content.status,
                    progress=content.progress,
                    due_at=_mysql_datetime(content.due_at),
                    completed_at=completed_at,
                )
                session.add(task)
                await session.flush()

                return {
                    "task_id": task.id,
                    "created": True,
                }
        except IntegrityError:
            # 两个相同 task_id 的请求并发创建时，主键只允许一个成功。
            async with self._mysql.session() as session:
                existing = await session.get(OfficeTask, content.task_id)
                if existing is not None and existing.meeting_id == scope.meeting_id:
                    return {
                        "task_id": existing.id,
                        "created": False,
                    }
            raise

    @invalidate_cache(_l4_progress_invalidation)
    async def update_task(self,request,content: OfficeTaskUpdateContent) -> dict[str, Any]:
        """
        更新办公任务的进度和状态。
        """

        scope = request.scope
        async with self._mysql.transaction() as session:
            stmt = (
                select(OfficeTask)
                .where(
                    OfficeTask.id == content.task_id,
                    OfficeTask.meeting_id == scope.meeting_id,
                )
                .with_for_update()
            )
            task = await session.scalar(stmt)
            if task is None:
                raise ValueError(f"办公任务不存在：{content.task_id}")

            old_status = task.status
            old_progress = task.progress
            completed_at = task.completed_at
            if content.status == "done" or content.progress == 100:
                completed_at = utc_now()
            # 原来已经完成的任务，现在重新更新，需清除completed_at字段的值
            elif old_status == "done" or old_progress == 100:
                completed_at = None

            stmt = (
                update(OfficeTask)
                .where(
                    OfficeTask.id == content.task_id,
                    OfficeTask.meeting_id == scope.meeting_id,
                )
                .values(
                    status=content.status,
                    progress=content.progress,
                    completed_at=completed_at,
                    updated_at=utc_now(),
                )
            )
            await session.execute(stmt)

            return {
                "task_id": task.id,
                "old_status": old_status,
                "new_status": content.status,
                "old_progress": old_progress,
                "new_progress": content.progress,
            }

    @cache_aside(
        key_builder=_l4_progress_cache_key,
        ttl_seconds=redis_config.l4_ttl_seconds,
        decoder=MemoryResponse.model_validate,
    )
    async def recall(self, request) -> MemoryResponse:
        """
        召回当前会议最近更新的办公任务
        """

        scope = request.scope
        async with self._mysql.session() as session:
            stmt = (
                select(OfficeTask)
                .where(OfficeTask.meeting_id == scope.meeting_id)
                .order_by(OfficeTask.updated_at.desc())
                .limit(request.recent_limit)
            )
            result = await session.scalars(stmt)
            tasks = list(result.all())

        return MemoryResponse(
            office_progress=[_task_dict(task) for task in tasks]
        )
