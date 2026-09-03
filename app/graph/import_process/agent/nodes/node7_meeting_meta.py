import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage
from app.graph.import_process.agent.state import ImportGraphState
from app.lm.llm_utils import get_llm_client
from app.memory.memory_manager import get_memory_manager, MemoryManager
from app.memory.models import MemoryWriteRequest, MemoryLayer, MemoryKind, MemoryScope
from app.utils.task_utils import add_running_task, add_done_task
from app.utils.logger import logger
from app.utils.load_prompt import load_prompt



class NewOfficeTask(BaseModel):
    title: str = Field(description="任务标题")
    description: str = Field(description="任务描述")
    owner: str | None = Field(description="任务负责人，未识别到则为None")
    due_at: datetime | None = Field(description="任务截止时间，未识别到则为None")

class NewOfficeTaskList(BaseModel):
    task_list: list[NewOfficeTask] = Field(description="会议内容新识别到的任务列表")


class MeetingExtract(BaseModel):
    summary: str = Field(description="整场会议的总结摘要")
    details : dict[str, Any] = Field(
        default_factory=dict,
        description="生成的动态会议数据，例如任务、项目里程碑、会议决定、风险等",
    )


def validate_content(state):
    """
    获取content,file_title
    :param state:
    :return: file_title, content
    """
    content = state.get("md_content")
    file_title = state.get("file_title")
    if not content :
        raise ValueError("md_content没有值")
    if not file_title:
        md_path = state.get("md_path")
        if not md_path:
            raise ValueError("file_title和md_path都没有值")
        file_title = Path(md_path).stem
    return file_title, content


def extract_meeting_task(context, file_title) -> NewOfficeTaskList:
    """
    从会议转录文档获取会议提出的新任务
    :param context:
    :param file_title:
    :return:
    """

    prompt = load_prompt("meeting_task_extract", context=context, file_title=file_title)
    llm = get_llm_client(model = "gpt-5")

    messages = [
        HumanMessage(content=prompt)
    ]
    structured_llm = llm.with_structured_output(NewOfficeTaskList,method="json_mode")
    response = structured_llm.invoke(messages)

    return response

def extract_meeting_meta(context, file_title) -> MeetingExtract:
    """
    从会议转录文档获取会议的摘要和多个动态会议描述字段
    :param context:
    :param file_title:
    :return:
    """

    prompt = load_prompt("meeting_meta_extract", context=context, file_title=file_title)
    llm = get_llm_client(model = "gpt-5")

    messages = [
        HumanMessage(content=prompt)
    ]
    structured_llm = llm.with_structured_output(MeetingExtract,method="json_mode")
    response = structured_llm.invoke(messages)

    return response


async def save_memory_episode(memory_manager: MemoryManager, state: ImportGraphState, meeting_meta: MeetingExtract):
    """
    保存本场会议的信息
    """
    scope = MemoryScope(
        meeting_id=state["meeting_id"],
    )

    event_id = f"meeting-episode:{state['meeting_id']}:{state['document_id']}"
    request = MemoryWriteRequest(
        event_id=event_id,
        layer=MemoryLayer.EPISODIC,
        kind=MemoryKind.MEETING_EPISODE,
        scope=scope,
        content=meeting_meta.model_dump()
    )
    await memory_manager.write(request=request)


async def save_memory_progress(memory_manager: MemoryManager, state: ImportGraphState, meeting_task_list: NewOfficeTaskList):
    """
    保存本场会议转录文档提出的新任务
    """
    scope = MemoryScope(
        meeting_id=state["meeting_id"]
    )
    for index, task in enumerate(meeting_task_list.task_list, start=1):
        raw_id = ":".join(
            [state["meeting_id"], state["document_id"], "office-task", str(index)]
        )
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, raw_id))
        event_id = f"office-task-created:{task_id}"
        request = MemoryWriteRequest(
            event_id=event_id,
            layer=MemoryLayer.OFFICE_PROGRESS,
            kind=MemoryKind.OFFICE_TASK_CREATED,
            scope=scope,
            content={
                "task_id": task_id,
                "title": task.title,
                "description": task.description,
                "owner": task.owner,
                "status": "todo",
                "progress": 0,
                "due_at": task.due_at,
            }
        )
        await memory_manager.write(request=request)

async def node_meeting_meta(state: ImportGraphState) -> ImportGraphState:
    """
    节点: 会议元数据提取 (node_meeting_meta)
    该场会议的摘要总结，新提出的工作任务列表，会议标题
    """
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name)
    try:

        file_title , content = validate_content(state)

        meeting_task_list = extract_meeting_task(content, file_title)
        meeting_meta = extract_meeting_meta(content,file_title)

        # 更新state
        state["meeting_summary"] = meeting_meta.summary

        state["new_office_task"] = [task.model_dump(mode="json") for task in meeting_task_list.task_list]

        # 更新记忆
        memory_manager = get_memory_manager()
        await save_memory_episode(memory_manager,state,meeting_meta)
        await save_memory_progress(memory_manager,state,meeting_task_list)

    except Exception as e:
        logger.error(f"[{node_name}] 执行过程中发生错误: {e}")
        raise
    finally:
        logger.info(f">>> [{node_name}]节点执行完成")
        add_done_task(state["task_id"], node_name)

    return state

