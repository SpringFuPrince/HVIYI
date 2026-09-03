import copy
from typing import Literal, TypedDict


class QueryGraphState(TypedDict, total=False):

    task_id: str
    meeting_id: str
    session_id: str

    # ===== 意图类型 =====
    intent: Literal["chat", "office"]

    # ===== 当前对话轮次 =====
    turn_id: str
    user_message_id: str
    assistant_message_id: str

    # ===== 用户问题 =====
    original_query: str
    rewritten_query: str

    # ===== 记忆召回 =====
    memory: dict

    # ===== 多路检索结果 =====
    embedding_chunks: list[dict]
    hyde_embedding_chunks: list[dict]
    web_search_docs: list[dict]

    # ===== 融合和重排 =====
    rrf_chunks: list[dict]
    reranked_docs: list[dict]

    # ===== 上下文和回答 =====
    context_text: str
    answer: str
    citations: list[dict]  # 答引用的文档、Chunk、页面或时间范围
    memory_warnings: list[str]

    # ===== 控制信息 =====
    error: str
    is_stream: bool

# ========================
# 默认状态（全部为空）
# ========================
query_graph_default_state: QueryGraphState = {
    "task_id": "",
    "meeting_id": "",
    "session_id": "",
    "intent": "office",
    "turn_id": "",
    "user_message_id": "",
    "assistant_message_id": "",
    "original_query": "",
    "rewritten_query": "",
    "memory": {},
    "embedding_chunks": [],
    "hyde_embedding_chunks": [],
    "web_search_docs": [],
    "rrf_chunks": [],
    "reranked_docs": [],
    "context_text": "",
    "answer": "",
    "citations": [],
    "memory_warnings": [],
    "error": "",
    "is_stream": False,
}


def create_query_default_state(**overrides) -> QueryGraphState:
    """
    创建查询流程的默认状态，支持覆盖字段
    """
    state = copy.deepcopy(query_graph_default_state)
    state.update(overrides)
    return state


def get_query_default_state() -> QueryGraphState:
    return copy.deepcopy(query_graph_default_state)


def copy_query_state(state: QueryGraphState, **overrides) -> QueryGraphState:
    """
    复制现有状态并可覆盖字段，深拷贝，不污染原数据
    """
    new_state = copy.deepcopy(state)
    new_state.update(overrides)
    return new_state


if __name__ == "__main__":
    # 测试
    state = create_query_default_state(
        session_id="test_001",
        original_query="华为P60怎么样?",
        is_stream=False
    )
    print("初始化状态：", state)

    # 复制状态
    new_state = copy_query_state(
        state,
        original_query="修改后的问题"
    )
    print("复制后的状态：", new_state)
