import sys

from app.memory.models import MemoryLayer, MemoryRecallRequest, MemoryScope
from app.utils.logger import logger
from app.graph.query_graph.agent.state import QueryGraphState
from app.memory.context_manager import get_context_manager
from app.utils.task_utils import add_running_task, add_done_task


async def node_build_context(state: QueryGraphState) -> QueryGraphState:
    """
    构建上下文
    :param state:
    :return:
    """
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name, state.get("is_stream", True))

    try:
        query = state.get("rewritten_query") or state.get("original_query", "")
        if not query:
            raise ValueError("缺少有效的用户问题")

        scope = MemoryScope(
            meeting_id=state["meeting_id"],
            session_id=state["session_id"],
        )
        request = MemoryRecallRequest(
            scope=scope,
            query=query,
            layers=set(MemoryLayer),  # L1～L4全部检索
            top_k=5,
        )

        # 如果是闲聊，没有检索，赋值为空列表
        rag_documents = state.get("reranked_docs", []) if state.get("intent") == "office" else []

        context_manager = get_context_manager()
        result = await context_manager.build(request=request,rag_documents=rag_documents)

        if result.memory.warnings:
            logger.warning(f"[{node_name}] 存在记忆召回警告: {result.memory.warnings}")

        state["context_text"] = result.context_text
        add_done_task(state["task_id"], node_name, state.get("is_stream", False))
        return state

    except Exception as e:
        logger.error(f"[{node_name}] 上下文构建失败，异常信息: {e}")
        raise
    finally:
        logger.info(f">>> [{node_name}]节点执行完成")
