import sys
from app.utils.task_utils import add_running_task, add_done_task
from app.utils.logger import logger

RRF_K = 60
RRF_TOP_K = 20

def normalize_milvus_hit(hit: dict) -> dict | None:
    """
    格式化 Milvus 检索结果
    """
    entity = hit.get("entity") or {}
    chunk_id = entity.get("chunk_id") or hit.get("id")
    content = entity.get("content")

    if not chunk_id or not content:
        return None

    return {
        "chunk_id": str(chunk_id),
        "document_id": entity.get("document_id"),
        "file_title": entity.get("file_title") or "",
        "source_type": entity.get("source_type") or "document",
        "source": entity.get("source_type") or "document",
        "title": entity.get("title") or entity.get("file_title") or "",
        "parent_title": entity.get("parent_title") or "",
        "text": content,
        "url": "",
        "retrieval_scores": {},
    }


def rrf(sources,top_k: int = RRF_TOP_K,rrf_k: int = RRF_K) -> list[dict]:
    """
    对多个召回结果进行加权融合排序
    :param sources:
    :param top_k:
    :param rrf_k:
    :return:
    """
    # 分数字典
    score_dict: dict[str, float] = {}
    document_dict: dict[str, dict] = {}

    for source_name, hits, weight in sources:
        for rank, hit in enumerate(hits, start=1):
            document = normalize_milvus_hit(hit)
            if document is None:
                continue

            chunk_id = document["chunk_id"]

            if chunk_id not in document_dict:
                document_dict[chunk_id] = document
            document_dict[chunk_id]["retrieval_scores"][source_name] = hit.get("distance")

            score_dict[chunk_id] = ( score_dict.get(chunk_id, 0.0) + (weight / (rrf_k + rank))  )

    results = []

    for chunk_id, document in document_dict.items():
        results.append({
            **document,
            "rrf_score": score_dict[chunk_id],
        })

    results.sort(key=lambda item: item["rrf_score"], reverse=True)
    return results[:top_k]


def node_rrf(state):
    """
    对双路检索的结果进行加权融合排序rrf（Reciprocal Rank Fusion）
    RRF = 1 / (k + score)  k是平滑参数
    """
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name, state.get("is_stream", True))
    try:
        # 多召回通道列表[(召回通道名,召回命中列表,权重),(召回通道名,召回命中列表,权重),)]
        rrf_chunks = rrf([
            ("embedding",state.get("embedding_chunks",[]),1.0),
            ("hyde",state.get("hyde_embedding_chunks",[]),1.0),
        ])
        """
        [
            {
                "chunk_id": "chunk-001",
                "document_id": "doc-001",
                "file_title": "会议记录.md",
                "source_type": "transcript_md",
                "source": "transcript_md",
                "title": "项目进度",
                "parent_title": "第二部分",
                "text": "会议讨论内容……",
                "url": "",
                "retrieval_scores": {
                    "hyde": 0.82
                },
                "rrf_score": 0.03226
            },
            ...
        ]
        """
        state["rrf_chunks"] = rrf_chunks
        add_done_task(state["task_id"], node_name, state.get("is_stream", False))
        return state

    except Exception as e:
        logger.error(f"[{node_name}] 对双路检索的结果进行加权融合排序失败，异常信息: {e}")
        raise
    finally:
        logger.info(f">>> [{node_name}]节点执行完成")
