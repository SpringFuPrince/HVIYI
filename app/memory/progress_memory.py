"""L4：按 meeting_id 保存办公任务的当前进度。"""
import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.clients.milvus_utils import (
    async_hybrid_search,
    create_hybrid_search_requests,
    get_async_milvus_client,
)
from app.conf.milvus_config import milvus_config
from app.db.memory_models import OfficeTask, utc_now
from app.lm.embedding_utils import generate_embeddings, generate_query_embeddings
from app.lm.llm_utils import get_llm_client
from app.memory.models import (
    MemoryKind,
    MemoryLayer,
    MemoryResponse,
    MemoryWriteRequest,
    OfficeTaskCreateContent,
    OfficeTaskUpdateContent,
)
from app.utils.escape_milvus_string_utils import escape_milvus_string
from app.utils.load_prompt import load_prompt
from app.utils.logger import logger


class L4RouteDecision(BaseModel):
    """
    L4根据用户问题识别出的内部执行动作
    """
    action: Literal["none", "create", "update", "recent", "semantic"] = "none"
    recent_limit: int = Field(default=10, ge=1, le=50, description="查询最近任务的数量")
    reason: str = ""


class NewOfficeTask(BaseModel):
    """
    识别到的新办公任务信息
    """
    title: str = Field(description="任务标题")
    description: str | None = None
    owner: str | None = None
    due_at: datetime | None = None


class NewOfficeTaskList(BaseModel):
    """
    识别到的新办公任务列表
    """
    have_task: bool = False
    task_list: list[NewOfficeTask] = Field(default_factory=list)



class OfficeTaskUpdateDecision(BaseModel):
    """
    将Milvus召回的任务中由模型选出的用户要操作的任务信息
    """
    should_update: bool = False
    task_id: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    status: Literal["todo", "done", "cancelled"] | None = None
    reason: str = ""


def _mysql_datetime(value: datetime | None) -> datetime | None:
    """把有时区时间统一转成MySQL DATETIME 无时区UTC的时间。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _task_dict(task: OfficeTask) -> dict[str, Any]:
    """
    把OfficeTask对象转成MemoryResponse可以直接返回的字典
    """

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


def _task_index_data(task: OfficeTask) -> dict[str, str]:
    """
    提取生成L4Milvus语义索引所需的文本字段
    """
    return {
        "task_id": task.id,
        "meeting_id": task.meeting_id,
        "title": task.title,
        "description": task.description or "",
        "owner": task.owner or "",
    }


def _task_search_text(task: dict[str, str]) -> str:
    """
    把任务的语义字段转换为用于向量化的规范文本
    """

    return "\n".join(
        [
            f"任务标题：{task['title']}",
            f"任务描述：{task['description'] or '无'}",
            f"负责人：{task['owner'] or '未指定'}",
        ]
    )


def _hit_entity(hit: dict[str, Any]) -> dict[str, Any]:
    """
    从Milvus召回结果中提取实体字段
    """
    entity = hit.get("entity")
    return entity if isinstance(entity, dict) else hit


class OfficeProgressMemory:
    def __init__(
        self,
        mysql_client,
        milvus_client=None,
        collection_name: str | None = None,
    ):
        self._mysql = mysql_client
        self._milvus = milvus_client or get_async_milvus_client()
        self._collection_name = collection_name or milvus_config.office_task_collection

    async def write(self, request):
        """
        写入结构化任务入口
        1. 会议转录任务创建
        2. L4内部识别完成后写入
        """
        # 创建任务
        if request.kind == MemoryKind.OFFICE_TASK_CREATED:
            content = OfficeTaskCreateContent.model_validate(request.content)
            return await self.create_task(request, content)

        # 更新任务进度
        if request.kind == MemoryKind.OFFICE_TASK_UPDATED:
            content = OfficeTaskUpdateContent.model_validate(request.content)
            return await self.update_task(request, content)

        raise ValueError(f"L4不支持kind={request.kind.value}")

    async def create_task(self,request,content: OfficeTaskCreateContent) -> dict[str, Any]:
        """
        创建办公任务
        """

        scope = request.scope

        created = False
        task_index_data: dict[str, str] | None = None

        try:
            async with self._mysql.transaction() as session:
                # 检查任务是否已经存在
                existing = await session.get(OfficeTask, content.task_id)
                # 如果任务存在，直接返回
                if existing is not None:
                    if existing.meeting_id != scope.meeting_id:
                        raise ValueError(f"任务ID已经存在：{content.task_id}")
                    task_index_data = _task_index_data(existing)
                # 不存在，正常创建
                else:
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
                    created = True
                    task_index_data = _task_index_data(task)
        # 处理并发创建任务
        except IntegrityError:
            # 两个相同 task_id 的请求并发创建时，只允许一个成功
            async with self._mysql.session() as session:
                existing = await session.get(OfficeTask, content.task_id)
                if existing is not None and existing.meeting_id == scope.meeting_id:
                    task_index_data = _task_index_data(existing)
                else:
                    raise

        if task_index_data is None:
            raise RuntimeError(f"办公任务创建失败：{content.task_id}")
        # 更新Milvus索引
        indexed = await self._try_index_task(task_index_data)
        return {
            "task_id": content.task_id,
            "created": created,
            "indexed": indexed,
        }

    async def update_task(self,request,content: OfficeTaskUpdateContent) -> dict[str, Any]:
        """
        更新办公任务进度
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

    async def recall(self, request) -> MemoryResponse:
        """
        L4统一入口
        根据用户问题决策要执行的操作：
        创建任务、更新任务、查询最近K条、语义查询某个任务或无需处理。
        """
        try:
            # 路由决策要执行的操作
            decision = await self._recognize_route(request)

            if decision.action == "none":
                return MemoryResponse()
            # 创建任务
            if decision.action == "create":
                return await self._create_tasks_from_query(request)
            # 更新任务进度
            if decision.action == "update":
                return await self._update_task_from_query(request)
            # 查询最近K条任务
            if decision.action == "recent":
                response = await self.recall_recent(
                    request,
                    limit=decision.recent_limit,
                )
                response.office_operation = {
                    "action": "recent",
                    "status": "retrieved",
                    "message": f"已召回最近{len(response.office_progress)}个任务",
                    "requested_limit": decision.recent_limit,
                    "count": len(response.office_progress),
                }
                return response
            # 语义查询任务
            if decision.action == "semantic":
                response = await self.recall_semantic(request)
                response.office_operation = {
                    "action": "semantic",
                    "status": "retrieved",
                    "message": f"已召回{len(response.office_progress)}个相关任务",
                    "count": len(response.office_progress),
                }
                return response

            raise ValueError(f"不支持的L4操作：{decision.action}")

        except Exception as exc:
            logger.exception(f"L4内部路由或执行失败：{exc}")
            return MemoryResponse(
                office_operation={
                    "action": "unknown",
                    "status": "failed",
                    "message": "办公任务处理失败，本轮没有确认任何任务变更",
                },
                warnings=["L4办公任务处理失败"],
            )

    async def _recognize_route(self, request) -> L4RouteDecision:
        """
        根据用户问题决策要执行的L4操作
        """

        prompt = load_prompt(
            "l4_route",
            original_query=self._original_query(request),
            rewritten_query=(request.query or "").strip(),
        )
        structured_llm = get_llm_client().with_structured_output(
            L4RouteDecision,
            method="json_mode",
        )
        result = await structured_llm.ainvoke([HumanMessage(content=prompt)])
        return L4RouteDecision.model_validate(result)

    async def _create_tasks_from_query(self, request) -> MemoryResponse:
        """
        从用户原始问题提取新任务并创建
        """

        turn_id = str(request.turn_id or "").strip()
        if not turn_id:
            return MemoryResponse(
                office_operation={
                    "action": "create",
                    "status": "failed",
                    "message": "缺少turn_id，未创建任务",
                },
                warnings=["L4创建任务缺少turn_id"],
            )

        prompt = load_prompt(
            "recognition_progress",
            original_query=self._original_query(request),
        )
        structured_llm = get_llm_client().with_structured_output(
            NewOfficeTaskList,
            method="json_mode",
        )
        result = await structured_llm.ainvoke([HumanMessage(content=prompt)])
        task_result = NewOfficeTaskList.model_validate(result)

        if not task_result.have_task or not task_result.task_list:
            return MemoryResponse(
                office_operation={
                    "action": "create",
                    "status": "needs_clarification",
                    "message": "没有识别到足够明确的新任务信息",
                }
            )

        created_tasks: list[dict[str, Any]] = []
        warnings: list[str] = []
        for index, task in enumerate(task_result.task_list, start=1):
            task_id = self._create_query_task_id(request, index)
            content = {
                "task_id": task_id,
                **task.model_dump(),
                "status": "todo",
                "progress": 0,
            }
            try:
                write_result = await self.write(
                    MemoryWriteRequest(
                        event_id=f"office-task-created:{task_id}",
                        layer=MemoryLayer.OFFICE_PROGRESS,
                        kind=MemoryKind.OFFICE_TASK_CREATED,
                        scope=request.scope,
                        content=content,
                    )
                )
                created_tasks.append({**content, **write_result})
                if not write_result.get("indexed", False):
                    warnings.append(f"任务{task_id}已创建，但Milvus索引失败")
            except Exception:
                logger.exception(f"创建L4任务失败：{task_id}")
                warnings.append(f"任务{task_id}创建失败")

        status = "created" if created_tasks else "failed"
        message = (
            f"成功创建{len(created_tasks)}个任务"
            if created_tasks
            else "没有任务创建成功"
        )
        return MemoryResponse(
            office_progress=created_tasks,
            office_operation={
                "action": "create",
                "status": status,
                "message": message,
                "count": len(created_tasks),
                "tasks": created_tasks,
            },
            warnings=warnings,
        )

    async def _update_task_from_query(self, request) -> MemoryResponse:
        """
        根据用户问题更新对应任务进度
        先语义召回候选任务，再识别出对应的任务更新
        """
        # 语义召回候选任务
        candidates_response = await self.recall_semantic(request)
        candidates = candidates_response.office_progress
        if not candidates:
            return MemoryResponse(
                office_operation={
                    "action": "update",
                    "status": "needs_clarification",
                    "message": "没有找到与当前描述相关的候选任务",
                }
            )

        # 识别出对应的任务更新
        cleaned_candidates = [self._clean_task(task) for task in candidates]
        prompt = load_prompt(
            "recognition_progress_update",
            original_query=self._original_query(request),
            rewritten_query=(request.query or "").strip(),
            candidates_json=json.dumps(
                cleaned_candidates,
                ensure_ascii=False,
                default=str,
            ),
        )
        structured_llm = get_llm_client().with_structured_output(
            OfficeTaskUpdateDecision,
            method="json_mode",
        )
        result = await structured_llm.ainvoke([HumanMessage(content=prompt)])
        decision = OfficeTaskUpdateDecision.model_validate(result)

        candidate_map = {
            str(task["task_id"]): task  for task in cleaned_candidates  if task.get("task_id")
        }
        if not decision.should_update or decision.task_id not in candidate_map:
            return MemoryResponse(
                office_progress=cleaned_candidates,
                office_operation={
                    "action": "update",
                    "status": "needs_clarification",
                    "message": decision.reason or "无法确定需要更新的任务",
                    "candidates": cleaned_candidates,
                },
            )

        current_task = candidate_map[decision.task_id]
        if decision.progress is None and decision.status is None:
            return MemoryResponse(
                office_progress=cleaned_candidates,
                office_operation={
                    "action": "update",
                    "status": "needs_clarification",
                    "message": "没有识别到明确的新进度或状态",
                    "candidates": cleaned_candidates,
                },
            )

        progress = (
            decision.progress  if decision.progress is not None  else int(current_task.get("progress") or 0)
        )
        status = decision.status or str(current_task.get("status") or "todo")
        if status == "done":
            progress = 100
        elif progress == 100:
            status = "done"

        # 写入更新任务进度
        write_result = await self.write(
            MemoryWriteRequest(
                event_id=(
                    f"office-task-updated:{decision.task_id}:"
                    f"{str(request.turn_id or 'unknown')}"
                ),
                layer=MemoryLayer.OFFICE_PROGRESS,
                kind=MemoryKind.OFFICE_TASK_UPDATED,
                scope=request.scope,
                content={
                    "task_id": decision.task_id,
                    "progress": progress,
                    "status": status,
                },
            )
        )
        updated_task = {
            **current_task,
            "progress": progress,
            "status": status,
        }
        return MemoryResponse(
            office_progress=[updated_task],
            office_operation={
                "action": "update",
                "status": "updated",
                "message": "任务进度更新成功",
                "task": updated_task,
                "change": write_result,
            },
        )

    @staticmethod
    def _original_query(request) -> str:
        """
        提取用户原始查询
        """
        return (request.original_query or request.query or "").strip()

    @staticmethod
    def _clean_task(task: dict[str, Any]) -> dict[str, Any]:
        """
        过滤掉任务中的None值，只保留必要的字段
        """
        fields = (
            "task_id",
            "meeting_id",
            "title",
            "description",
            "owner",
            "status",
            "progress",
            "due_at",
        )
        return {
            field: task.get(field) for field in fields if task.get(field) is not None
        }

    @staticmethod
    def _create_query_task_id(request, index: int) -> str:
        """
        创建查询任务的唯一ID
        """
        raw = ":".join(
            [
                request.scope.meeting_id,
                str(request.scope.session_id or ""),
                str(request.turn_id),
                "office-task",
                str(index),
            ]
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))

    async def recall_recent(self, request, limit: int | None = None) -> MemoryResponse:
        """
        从MySQL召回当前会议最近更新的任务
        """
        scope = request.scope
        actual_limit = limit or request.l4_recent_limit
        async with self._mysql.session() as session:
            stmt = (
                select(OfficeTask)
                .where(OfficeTask.meeting_id == scope.meeting_id)
                .order_by(OfficeTask.updated_at.desc())
                .limit(actual_limit)
            )
            result = await session.scalars(stmt)
            tasks = list(result.all())

        return MemoryResponse(
            office_progress=[_task_dict(task) for task in tasks]
        )

    async def recall_semantic(self, request, limit: int | None = None) -> MemoryResponse:
        """
        语义召回办公任务
        用Milvus查找相关task_id，再从MySQL读取任务
        """

        query = request.query.strip()
        if not query:
            return MemoryResponse()
        if not self._collection_name:
            raise ValueError("缺少OFFICE_TASK_COLLECTION环境变量配置")

        actual_limit = limit or request.l4_top_k

        embeddings = await asyncio.to_thread(generate_query_embeddings, [query])
        filter_expression = (
            f'meeting_id == "{escape_milvus_string(request.scope.meeting_id)}"'
        )
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
            limit=actual_limit,
        )
        search_result = await async_hybrid_search(
            client=self._milvus,
            collection_name=self._collection_name,
            reqs=search_requests,
            ranker_weights=(0.5, 0.5),
            norm_score=True,
            limit=actual_limit,
            output_fields=["task_id"],
        )

        hits = search_result[0] if search_result else []
        task_ids: list[str] = []
        for hit in hits:
            entity = _hit_entity(hit)
            task_id = entity.get("task_id") or hit.get("id")
            if task_id and str(task_id) not in task_ids:
                task_ids.append(str(task_id))
        if not task_ids:
            return MemoryResponse()

        async with self._mysql.session() as session:
            stmt = select(OfficeTask).where(
                OfficeTask.id.in_(task_ids),
                OfficeTask.meeting_id == request.scope.meeting_id,
            )
            result = await session.scalars(stmt)
            tasks_by_id = {task.id: task for task in result.all()}

        ordered_tasks = [
            tasks_by_id[task_id]
            for task_id in task_ids
            if task_id in tasks_by_id
        ]
        return MemoryResponse(
            office_progress=[_task_dict(task) for task in ordered_tasks]
        )

    async def _try_index_task(self, task: dict[str, str]) -> bool:
        """
        MySQL写入后，更新Milvus，失败不回滚任务状态
        """

        try:
            if not self._collection_name:
                raise ValueError("缺少OFFICE_TASK_COLLECTION环境变量配置")
            search_text = _task_search_text(task)
            embeddings = await asyncio.to_thread(generate_embeddings, [search_text])
            await self._milvus.upsert(
                collection_name=self._collection_name,
                data=[
                    {
                        "task_id": task["task_id"],
                        "meeting_id": task["meeting_id"],
                        "title": task["title"],
                        "dense_vector": embeddings["dense"][0],
                        "sparse_vector": embeddings["sparse"][0],
                    }
                ],
            )
            return True
        except Exception:
            logger.exception(
                f"L4办公任务写入MySQL成功，但Milvus更新失败：{task['task_id']}"
            )
            return False
