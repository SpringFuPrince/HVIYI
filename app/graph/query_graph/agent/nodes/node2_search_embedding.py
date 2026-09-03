import sys

from app.conf.milvus_config import milvus_config
from app.utils.escape_milvus_string_utils import escape_milvus_string
from app.utils.task_utils import add_running_task, add_done_task
from app.lm.embedding_utils import generate_embeddings
from app.clients.milvus_utils import create_hybrid_search_requests, hybrid_search, get_milvus_client
from app.utils.logger import logger
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

def required_state_value(state,field_name) -> str:
    value = state.get(field_name)
    if not isinstance(value, str) or not value.strip():
        logger.info(f"State 缺少有效字段：{field_name}")
        raise ValueError(f"State 缺少有效字段：{field_name}")
    return value


def build_retrieval_expr_meeting(state) -> str:
    # 本地版的文档检索必须限定在一场会议内。
    meeting_id = required_state_value(state,"meeting_id")

    # 转换为安全的 Milvus 字符串
    safe_meeting_id = escape_milvus_string(meeting_id)
    return (
        f'meeting_id == "{safe_meeting_id}"'
    )


def node_search_embedding(state):
    """
    改写问题的向量检索
    {
        "embedding_chunks": [chunks]
    }
    """
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name, state.get("is_stream", True))

    try:
        rewritten_query = state.get("rewritten_query")
        query_embeddings = generate_embeddings([rewritten_query])

        # 即使绕过 API 直接运行 Graph，也不允许退化为跨会议检索。
        expr = build_retrieval_expr_meeting(state)
        logger.info("按meeting_id检索当前会议文档")
        # 创建混合查询请求对象 AnnSearchRequest
        hybrid_search_requests = create_hybrid_search_requests(
            dense_vector=query_embeddings['dense'][0],
            sparse_vector=query_embeddings['sparse'][0],
            expr=expr,
            limit=20
        )
        # 混合查询
        milvus_client = get_milvus_client()
        resp = hybrid_search(
            client=milvus_client,
            collection_name=milvus_config.chunks_collection,
            reqs=hybrid_search_requests,
            ranker_weights=(0.6, 0.4),
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
        """
           [
             [
                {
                "id": "chunk主键",
                "distance": 0.82,
                "entity": {
                    "chunk_id": "...",
                    "document_id": "...",
                    "file_title": "会议记录",
                    "source_type": "transcript_md",
                    "content": "会议讨论内容……",
                    "title": "项目进度",
                    "parent_title": "第二部分",
                },
                {
                "id": "chunk主键",
                .....
                }
             ],
             [
                .....
             ]
           ]
    
        """
        embedding_chunks = resp[0] if resp else []
        state["embedding_chunks"] = embedding_chunks
        add_done_task(state["task_id"], node_name, state.get("is_stream", False))
        return {"embedding_chunks":embedding_chunks}

    except Exception as e:
        logger.error(f"[{node_name}] 问题向量检索失败，异常信息: {e}")
        raise
    finally:
        logger.info(f">>> [{node_name}]节点执行完成")
