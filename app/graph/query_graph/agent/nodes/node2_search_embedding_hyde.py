import sys

from langchain_core.messages import HumanMessage

from app.utils.escape_milvus_string_utils import escape_milvus_string
from app.utils.task_utils import add_running_task, add_done_task
from app.lm.llm_utils import *
from app.lm.embedding_utils import *
from app.clients.milvus_utils import *
from app.utils.logger import logger
from app.utils.load_prompt import load_prompt
from dotenv import load_dotenv

load_dotenv()


def required_state_value(state,field_name) -> str:
    value = state.get(field_name)
    if not isinstance(value, str) or not value.strip():
        logger.info(f"State 缺少有效字段：{field_name}")
        raise ValueError(f"State 缺少有效字段：{field_name}")
    return value

def create_hyde_doc(rewritten_query):
    """
    调用模型根据问题，生成一份答案
    :param rewritten_query:  问题
    :return: 答案字符串
    """
    llm = get_llm_client()

    # 加载提示词
    hyde_prompt = load_prompt("hyde_prompt",rewritten_query = rewritten_query)

    messages = [
        HumanMessage(content=hyde_prompt)
    ]
    # 发起请求
    response = llm.invoke(messages)
    hyde_doc = response.content
    logger.info(f"使用模型生成假设性答案，问题：{rewritten_query},答案：{hyde_doc}")
    return hyde_doc

def build_retrieval_expr_meeting(state) -> str:
    # 构建检索表达式，筛选同会议的向量

    meeting_id = required_state_value(state,"meeting_id")

    # 转换为安全的 Milvus 字符串
    safe_meeting_id = escape_milvus_string(meeting_id)

    return (
        f'meeting_id == "{safe_meeting_id}"'
    )



def search_embedding_hyde(state,rewritten_query, hyde_doc):
    """
    根据问题+假设性答案查询向量数据库，进行混合查询
    :return: [[] -> 结果  id 分数 实体列信息 ]
    """
    query_str = rewritten_query + hyde_doc
    embeddings = generate_embeddings([query_str])

    # 即使绕过 API 直接运行 Graph，也不允许退化为跨会议检索。
    expr = build_retrieval_expr_meeting(state)
    logger.info("按meeting_id检索当前会议文档")

    # 创建混合查询请求对象 AnnSearchRequest
    hybrid_search_requests = create_hybrid_search_requests(
        dense_vector=embeddings['dense'][0],
        sparse_vector=embeddings['sparse'][0],
        expr=expr,
        limit=20
    )
    # 混合查询
    milvus_client = get_milvus_client()
    resp = hybrid_search(
        client= milvus_client,
        collection_name=milvus_config.chunks_collection,
        reqs=hybrid_search_requests,
        ranker_weights=(0.9, 0.1),
        norm_score=True,
        limit=20,
        output_fields=[
            "chunk_id",
            "document_id",
            "file_title",
            "source_type",
            "content",
            "title",
            "parent_title",
        ]
    )

    result = resp[0] if resp else []
    # logger.info(f"HyDE检索结果：{result}")
    return result

def node_search_embedding_hyde(state):
    """
    HyDE检索
    （Hypothetical Document Embedding）
    先让 LLM 生成伪答案，再对答案进行向量检索，提高召回率。
    """
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name, state.get("is_stream", True))
    try:
        rewritten_query = state.get("rewritten_query")
        # 生成伪答案
        hyde_doc = create_hyde_doc(rewritten_query)

        hyde_embedding_chunks = search_embedding_hyde(state,rewritten_query,hyde_doc)

        add_done_task(state["task_id"], node_name, state.get("is_stream", False))
        return {"hyde_embedding_chunks":hyde_embedding_chunks}
    except Exception as e:
        logger.error(f"[{node_name}] HyDE检索失败，异常信息: {e}")
        raise
    finally:
        logger.info(f">>> [{node_name}]节点执行完成")

