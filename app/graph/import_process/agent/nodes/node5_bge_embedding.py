import sys
from app.graph.import_process.agent.state import ImportGraphState
from app.lm.embedding_utils import generate_embeddings
from app.utils.task_utils import add_running_task,add_done_task
from app.utils.logger import logger

def node_bge_embedding(state: ImportGraphState) -> ImportGraphState:
    """
    节点: 向量化 (node_bge_embedding)
    1. 加载 BGE-M3 模型。
    2. 对每个 Chunk 的文本进行 Dense (稠密) 和 Sparse (稀疏) 向量化。
    3. 准备好写入 Milvus 的数据格式。
    """
    # 获取当前节点名称，用于日志和任务状态记录
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name)

    try:
        # 1.获取要生成向量的chunks
        chunks = state.get("chunks")
        if not chunks or not isinstance(chunks, list):
            logger.error("chunks数据无效，请检查数据格式")
            raise  ValueError("chunks数据无效，请检查数据格式")

        # 2.给每个chunk生成向量
        # 2.1 获取嵌入式模型的客户端
        # 2.2 批量生成向量

        final_chunks = [] # 存储处理完的chunk带有向量
        batch_size = 5 # 一次embedding chunk个数

        for i in range(0,len(chunks),batch_size):
            # i+1  i+batch_size (步长)
            # 本次批量处理的chunk
            batch_items = chunks[i:i+batch_size]
            # 定义当前批次的字符串！
            embedding_texts_list = [item.get("content") for item in batch_items]

            # 当前批次生成的向量
            result = generate_embeddings(embedding_texts_list)
            # 当前批次的chunk添加向量即可
            #  # 完善chunk的属性添加稠密和稀疏向量
            for i, chunk in enumerate(batch_items):
                chunk_item = chunk.copy()
                chunk_item['dense_vector'] = result['dense'][i]
                chunk_item['sparse_vector'] = result['sparse'][i]
                final_chunks.append(chunk_item)
        state['chunks'] = final_chunks

    except Exception as e:
        logger.error(f"BGE-M3向量化节点执行失败：{str(e)}", exc_info=True)
    finally:
        logger.info(f"--- BGE-M3 向量化处理完成，共处理 {len(final_chunks)} 条文本切片 ---")
        add_done_task(state["task_id"], node_name)
    return state