from langgraph.graph import StateGraph, END
from app.graph.query_graph.agent.state import QueryGraphState
from app.graph.query_graph.agent.nodes.node1_rewrite_and_intent import node_rewrite_and_intent
from app.graph.query_graph.agent.nodes.node4_rerank import node_rerank
from app.graph.query_graph.agent.nodes.node3_rrf import node_rrf
from app.graph.query_graph.agent.nodes.node2_search_embedding import node_search_embedding
from app.graph.query_graph.agent.nodes.node2_search_embedding_hyde import node_search_embedding_hyde
from app.graph.query_graph.agent.nodes.node2_web_search_mcp import node_web_search_mcp
from app.graph.query_graph.agent.nodes.node5_build_context import node_build_context
from app.graph.query_graph.agent.nodes.node6_answer_output import node_answer_output
from app.graph.query_graph.agent.nodes.node7_save_memory import node_save_memory


builder = StateGraph(QueryGraphState)

builder.add_node("node_rewrite_and_intent", node_rewrite_and_intent)  # 改写并确认意图
builder.add_node("node_search_embedding", node_search_embedding)  # 向量搜索
builder.add_node("node_search_embedding_hyde", node_search_embedding_hyde)
builder.add_node("node_web_search_mcp", node_web_search_mcp)
builder.add_node("node_rrf", node_rrf)  # 排序
builder.add_node("node_rerank", node_rerank)  # 重排
builder.add_node("node_build_context", node_build_context)  # 构建上下文窗口
builder.add_node("node_answer_output", node_answer_output)  # 生成
builder.add_node("node_save_memory", node_save_memory)  # 保存记忆

builder.set_entry_point("node_rewrite_and_intent")

def route_with_intent(state: QueryGraphState):
    query_plan = state["query_plan"]
    res = set()
    if query_plan["query_unclear"]:
        return "node_answer_output"
    else:
        if query_plan["local_retrieval"]:
            res.update({"node_search_embedding", "node_search_embedding_hyde"})
        if query_plan["need_web_search"]:
            res.add("node_web_search_mcp")
        return res if res else "node_build_context"

## 1. 意图路由
builder.add_conditional_edges(
    "node_rewrite_and_intent",
    route_with_intent,
    {
        "node_build_context": "node_build_context",
        "node_answer_output": "node_answer_output",
        "node_search_embedding": "node_search_embedding",
        "node_search_embedding_hyde": "node_search_embedding_hyde",
        "node_web_search_mcp": "node_web_search_mcp",
    }
)

builder.add_edge("node_search_embedding", "node_rrf")
builder.add_edge("node_search_embedding_hyde", "node_rrf")
builder.add_edge("node_web_search_mcp", "node_rrf")
builder.add_edge("node_rrf", "node_rerank")
builder.add_edge("node_rerank", "node_build_context")
builder.add_edge("node_build_context", "node_answer_output")
builder.add_edge("node_answer_output", "node_save_memory")
builder.add_edge("node_save_memory", END)


query_graph = builder.compile()
