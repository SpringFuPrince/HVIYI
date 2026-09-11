import sys
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from app.conf.lm_config import lm_config
from app.graph.query_graph.agent.state import QueryGraphState
from app.lm.llm_utils import get_llm_client
from app.memory.memory_manager import get_memory_manager
from app.memory.models import MemoryScope, MemoryLayer
from app.utils.load_prompt import load_prompt
from app.utils.task_utils import add_running_task, add_done_task
from app.utils.logger import logger

# 获取最近会话数量，用于问题重写，补充上下文信息，消除指代歧义
RECENT_MESSAGE_LIMIT = 10


class QueryPlan(BaseModel):
    recall_layers: set[MemoryLayer]

    query_unclear: bool = False
    local_retrieval: bool = True
    need_web_search: bool = False

    write_l2_portrait: bool = False
    write_l3_conversation: bool = False

class RewriteAndIntentResult(BaseModel):
    rewritten_query: str = Field( description=("结合最近会话补全指代、省略信息后,得到的完整、适合检索的问题"))
    query_plan: QueryPlan = Field(...,description="根据用户意图和上下文,生成的路由计划")


def required_state_value(state,field_name) -> str:
    value = state.get(field_name)
    if not isinstance(value, str) or not value.strip():
        logger.info(f"State 缺少有效字段：{field_name}")
        raise ValueError(f"State 缺少有效字段：{field_name}")
    return value



def format_recent_messages(messages: list[dict]) -> str:
    """
    格式化最近会话
    """
    if not messages:
        return "无历史会话"

    lines = []

    for message in messages:
        content = message.get("content", "")
        lines.append(f"{message.get('role')}：{content}")

    return "\n".join(lines)


async def get_recent_messages(state: QueryGraphState, scope: MemoryScope):
    """
    获取最近会话
    """
    memory_manager = get_memory_manager()
    original_query = required_state_value(state, "original_query")

    recent_messages = await memory_manager.get_recent_messages(scope=scope, limit=RECENT_MESSAGE_LIMIT)
    # 格式化最近会话
    recent_messages_text = format_recent_messages(recent_messages)

    return recent_messages_text, original_query


async def rewrite_and_intent(recent_messages_text: str, original_query: str):
    """
    问题重写和意图识别
    """
    prompt = load_prompt(
        "query_rewrite_and_intent",
        recent_messages=recent_messages_text,
        original_query=original_query,
    )

    llm = get_llm_client(model=lm_config.llm_model)

    structured_llm = llm.with_structured_output(
        RewriteAndIntentResult,
        method="json_mode",
    )

    result = await structured_llm.ainvoke( [HumanMessage(content=prompt)] )
    return result



async def node_rewrite_and_intent(state):
    """
    问题重写和意图识别
    """
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name, state.get("is_stream", True))
    try:
        # 构造会话范围
        scope = MemoryScope(
            meeting_id=required_state_value(state, "meeting_id"),
            session_id=required_state_value(state, "session_id"),
        )
        recent_messages_text, original_query = await get_recent_messages(state, scope)

        result = await rewrite_and_intent(recent_messages_text, original_query)

        state["rewritten_query"] = result.rewritten_query.strip()
        state["query_plan"] = result.query_plan.model_dump(mode="json")

        add_done_task(state["task_id"], node_name, state.get("is_stream", False))
        return state

    except Exception as e:
        logger.error(f"[{node_name}] 问题重写和意图识别失败，异常信息: {e}")
        raise
    finally:
        logger.info(f">>> [{node_name}]节点执行完成")

