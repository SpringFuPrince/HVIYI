import uuid
import sys
from pymilvus import DataType
from app.graph.import_process.agent.state import ImportGraphState
from app.clients.milvus_utils import get_milvus_client
from app.utils.task_utils import add_running_task, add_done_task
from app.utils.logger import logger
from app.conf.milvus_config import milvus_config

def build_milvus_rows(state: ImportGraphState) -> list[dict]:
    """
    组装Milvus数据
    :param state:
    :return:
    """

    required_fields = (
        "meeting_id",
        "document_id",
        "file_title",
        "file_type",
    )
    for field in required_fields:
        value = state.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"缺少有效字段：{field}")

    chunks = state.get("chunks")
    if not chunks:
        raise ValueError("chunks 为空")

    rows = []
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            raise TypeError(f"第 {index} 个 Chunk 不是字典")

        content = chunk.get("content")
        dense_vector = chunk.get("dense_vector")
        sparse_vector = chunk.get("sparse_vector")

        if not isinstance(content, str) or not content.strip():
            logger.warning(f"第 {index} 个 Chunk 内容为空，跳过")
            continue

        if dense_vector is None or len(dense_vector) != 1024:
            raise ValueError(f"第 {index} 个 Chunk 的稠密向量维度错误")

        if not isinstance(sparse_vector, dict):
            raise ValueError(f"第 {index} 个 Chunk 缺少有效稀疏向量")

        chunk_id = str(uuid.uuid4())

        row = {
            "chunk_id": chunk_id,
            "meeting_id": state["meeting_id"],
            "document_id": state["document_id"],
            "file_title": state["file_title"],
            "source_type": state["file_type"],
            "content": content,
            "title": chunk.get("title") or chunk.get("parent_title") or "",
            "parent_title": chunk.get("parent_title") or chunk.get("title") or "",
            "dense_vector": dense_vector,
            "sparse_vector": sparse_vector,
        }

        rows.append(row)

    return rows

def prepare_collections(state):
    """
    创建chunks对应的集合
    :param state:
    :return:
    """

    milvus_client = get_milvus_client()
    if milvus_client is None:
        logger.error("无法连接 Milvus")
        raise RuntimeError("无法连接 Milvus")
    # 判断是否存在集合
    if milvus_client.has_collection(collection_name=milvus_config.chunks_collection):
        description = milvus_client.describe_collection(
            collection_name=milvus_config.chunks_collection,
        )
        fields = {
            field.get("name") or field.get("field_name")
            for field in description.get("fields", [])
            if isinstance(field, dict)
        }
        expected_fields = {
            "chunk_id", "meeting_id", "document_id", "file_title", "source_type",
            "content", "title", "parent_title", "dense_vector", "sparse_vector",
        }
        stale_scope_fields = {"tenant_id", "user_id"} & fields
        if not expected_fields.issubset(fields) or stale_scope_fields:
            raise RuntimeError(
                "文档向量集合是旧版Schema，不能写入本地版数据；"
                f"请使用新的CHUNKS_COLLECTION或迁移集合：{milvus_config.chunks_collection}"
            )
    else:
        # 创建集合
        schema = milvus_client.create_schema(
            auto_id=False,
            enable_dynamic_field=False,
        )

        schema.add_field(field_name="chunk_id", datatype=DataType.VARCHAR, max_length=64, is_primary=True)
        schema.add_field(field_name="meeting_id", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="document_id", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="file_title", datatype=DataType.VARCHAR, max_length=65535)
        schema.add_field(field_name="source_type", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="content", datatype=DataType.VARCHAR, max_length=65535)
        schema.add_field(field_name="title", datatype=DataType.VARCHAR, max_length=65535)
        schema.add_field(field_name="parent_title", datatype=DataType.VARCHAR, max_length=65535)
        schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=1024)
        schema.add_field(field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR)
        # 配置索引
        index_params = milvus_client.prepare_index_params()

        for field_name in ("meeting_id","document_id"):
            index_params.add_index(
                field_name=field_name,
                index_name=f"{field_name}_index",
                index_type="INVERTED",
            )

        # 稠密向量索引
        index_params.add_index(
            field_name="dense_vector",
            index_name="dense_vector_index",
            index_type="HNSW",
            metric_type="COSINE",
            params={
                "M": 32,
                "efConstruction": 300,
            },
        )

        # 稀疏向量索引
        index_params.add_index(
            field_name="sparse_vector",
            index_name="sparse_vector_index",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
            params={
                "inverted_index_algo": "DAAT_MAXSCORE",
            },
        )

        milvus_client.create_collection(
            collection_name=milvus_config.chunks_collection,
            schema=schema,  # 字段
            index_params=index_params  # 索引
        )
    return milvus_client



def insert_collections(milvus_client,rows: list[dict],batch_size: int = 100) -> int:
    """
    分批插入或更新，返回本次成功写入的行数
    :param rows:
    :param batch_size:
    :return:
    """
    if milvus_client is None:
        raise RuntimeError("Milvus 客户端不可用")

    if batch_size <= 0:
        raise ValueError("batch_size 必须大于 0")

    collection_name = milvus_config.chunks_collection
    if not collection_name:
        raise ValueError("未配置集合名称")

    milvus_client.load_collection(
        collection_name=collection_name,
    )

    total_count = 0

    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]

        result = milvus_client.upsert(
            collection_name=collection_name,
            data=batch,
        )

        count = result["upsert_count"]
        total_count += count
        logger.info(f"Milvus 写入进度：{total_count}/{len(rows)}")

    return total_count


def node_import_milvus(state: ImportGraphState) -> ImportGraphState:
    """
    导入Milvus (node_import_milvus)

    """
    # 1. 进入的日志和任务状态的配置
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name)

    try:
        rows = build_milvus_rows(state)

        # 2. 准备集合
        milvus_client = prepare_collections(state)
        # 插入数据
        count = insert_collections(milvus_client,rows)
        logger.info(f"[{node_name}] 写入成功，共 {count} 条")

    except Exception as e:
        logger.error(f">>> [{node_name}]导入chunks对应的向量数据库发生了异常，异常信息：{e}")
        raise
    finally:
        logger.info(f">>> [{node_name}]开始结束了！")
        add_done_task(state["task_id"], node_name)
    return state


