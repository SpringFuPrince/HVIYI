import hashlib
from app.utils.task_utils import *

import sys
from app.lm.reranker_utils import get_reranker_model
from app.utils.logger import logger
from app.utils.task_utils import add_running_task


# 动态 TopK 硬上限：最多取前 N 条（<=10）
RERANK_MAX_TOPK: int = 10
# 最小 TopK：至少保留前 N 条（>=1，且 <= RERANK_MAX_TOPK）
RERANK_MIN_TOPK: int = 1
# 断崖阈值（相对）
RERANK_GAP_RATIO: float = 0.25
# 断崖阈值（绝对）
RERANK_GAP_ABS: float = 0.5 # 最大间断分值

def normalize_web_doc(doc: dict) -> dict | None:
    """
    格式化 MCP 联网搜索文档
    """
    text = str(doc.get("snippet") or doc.get("content") or "").strip()

    if not text:
        return None

    title = str(doc.get("title") or "")
    url = str(doc.get("url") or "")
    identity = url or f"{title}|{text}"
    chunk_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    return {
        "chunk_id": f"web_{chunk_id}",
        "document_id": None,
        "file_title": title,
        "source_type": "web",
        "source": "web",
        "title": title,
        "parent_title": "",
        "text": text,
        "url": url,
        "retrieval_scores": {
            "web": doc.get("score")
        },
        "rrf_score": None,
    }

def merge_rrf_web(state) -> list[dict]:
    """
    将 rrf合并的列表和web搜索的数据列表合并
    :param state:
    :return:
    """
    candidates = []

    for document in state.get("rrf_chunks") or []:
        if document.get("text"):
            candidates.append(dict(document))

    for web_doc in state.get("web_search_docs") or []:
        normalized = normalize_web_doc(web_doc)
        if normalized:
            candidates.append(normalized)

    logger.info(f"Rerank候选文档数量: {len(candidates)}")
    return candidates

def rerank_doc_list(candidates: list[dict], state):
    """
    使用 reranker 模型对文档列表进行精排
    :param doc_list:
    :param state:
    :return:
    """
    query = state.get("rewritten_query") or state.get("original_query")

    # 将数据转换为 reranker 模型需要的格式
    questions_pairs = [[query, candidate["text"]] for candidate in candidates]
    # 获取模型
    reranker_model = get_reranker_model()
    # 计算分数
    # normalize=True 默认False 分的范围不确定 + 0 -   3  0  -3
    #                   True  分 缩放到 0 - 1 分
    scores  = reranker_model.compute_score(questions_pairs,normalize = True)

    results = [ { **candidate,"score": float(score) } for candidate, score in zip(candidates, scores) ]
    results.sort( key=lambda item: item["score"], reverse=True )
    logger.info(f"已经完成排序和打分")
    return results



def topk_and_gap(rerank_score_list):
    """
    对 rerank_score_list 进行放断崖处理和top_k提取
    :param rerank_score_list:
    :return:
    """
    max_topk = RERANK_MAX_TOPK  # 至多获取的元素的数量
    min_topk = RERANK_MIN_TOPK  # 至少获取的元素数量
    gap_abs = RERANK_GAP_ABS  # 断崖的分差    0.9  0.64 =》 0.26 （分）
    gap_ratio = RERANK_GAP_RATIO  # 断崖的百分比  （1-2）/ 1  =》 0.25 保留


    top_k = min(max_topk,len(rerank_score_list))
    if top_k > min_topk:
        for index in range(min_topk - 1, top_k -1):
            score_1 = rerank_score_list[index].get('score',0.0)
            score_2 = rerank_score_list[index + 1].get('score',0.0)

            gap = score_1 - score_2
            rel = gap / (score_1 + 1e-8)
            if gap >= gap_abs or rel >= gap_ratio:
                # 断崖
                logger.info(f"数据集合{index}和{index + 1}的位置发生了断崖，结束循环！！")
                top_k = index + 1
                break
    topk_doc_list = rerank_score_list[:top_k]
    logger.info(f"最终截取的长度：{top_k}")
    # 5.返回结果
    return topk_doc_list

def node_rerank(state):
    """
    使用 reranker 模型对文档列表进行精排
    :param state:
    :return:
    """
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name, state.get("is_stream", True))
    try:
        candidates = merge_rrf_web(state)
        reranked_candidates = rerank_doc_list(candidates, state)
        reranked_docs = topk_and_gap(reranked_candidates)

        state["reranked_docs"] = reranked_docs

        add_done_task(state["task_id"], node_name, state.get("is_stream", False))
        return state
    except Exception as e:
        logger.error(f"[{node_name}] 问题精排失败，异常信息: {e}")
        raise
    finally:
        logger.info(f">>> [{node_name}]节点执行完成")


