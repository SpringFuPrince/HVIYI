import asyncio

from pymilvus import (
    AnnSearchRequest,
    AsyncMilvusClient,
    DataType,
    MilvusClient,
    WeightedRanker,
)
from app.conf.milvus_config import milvus_config
from app.utils.logger import logger

# 全局Milvus客户端实例，实现单例复用
_milvus_client = None
_async_milvus_client = None
_init_lock = None
_conversation_memory_initialized = False
_office_task_initialized = False


def get_milvus_client():
    """
    Milvus客户端单例获取方法
    实现客户端连接复用，避免重复创建连接消耗资源
    :return: MilvusClient实例，连接失败返回None
    """
    try:
        global _milvus_client
        # 单例判断：未初始化则创建新连接
        if _milvus_client is None:
            milvus_uri = milvus_config.milvus_url
            # 校验Milvus连接地址配置
            if not milvus_uri:
                logger.error("Milvus客户端连接失败：缺少MILVUS_URL环境变量配置")
                return None
            # 初始化Milvus客户端
            _milvus_client = MilvusClient(uri=milvus_uri)
            logger.info("Milvus客户端连接成功")
        return _milvus_client
    except Exception as e:
        logger.error(f"Milvus客户端连接异常：{str(e)}", exc_info=True)
        return None


def get_async_milvus_client() -> AsyncMilvusClient:
    """获取L3记忆使用的异步Milvus客户端，不在模块导入时连接。"""

    global _async_milvus_client
    if _async_milvus_client is not None:
        return _async_milvus_client
    if not milvus_config.milvus_url:
        raise ValueError("缺少MILVUS_URL环境变量配置")

    kwargs = {"uri": milvus_config.milvus_url}
    if milvus_config.milvus_token:
        kwargs["token"] = milvus_config.milvus_token
    _async_milvus_client = AsyncMilvusClient(**kwargs)
    return _async_milvus_client


async def initialize_conversation_memory_collection() -> None:
    """应用启动时创建L3历史对话集合和稠密向量索引和稀疏向量索引"""

    global _init_lock, _conversation_memory_initialized

    if _conversation_memory_initialized:
        return
    if _init_lock is None:
        _init_lock = asyncio.Lock()

    collection_name = milvus_config.conversation_memory_collection
    if not collection_name:
        raise ValueError("缺少CONVERSATION_MEMORY_COLLECTION环境变量配置")

    async with _init_lock:
        if _conversation_memory_initialized:
            return

        client = get_async_milvus_client()
        if await client.has_collection(collection_name=collection_name):
            description = await client.describe_collection(
                collection_name=collection_name
            )
            field_names = {
                field.get("name") or field.get("field_name")
                for field in description.get("fields", [])
                if isinstance(field, dict)
            }
            expected_fields = {
                "memory_id",
                "meeting_id",
                "session_id",
                "user_message_id",
                "assistant_message_id",
                "conversation_time",
                "status",
                "dense_vector",
                "sparse_vector",
            }
            _conversation_memory_initialized = True
            logger.info(f"L3历史对话集合已初始化：{collection_name}")
            return

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("memory_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("meeting_id", DataType.VARCHAR, max_length=64)
        schema.add_field("session_id", DataType.VARCHAR, max_length=64)
        schema.add_field("user_message_id", DataType.VARCHAR, max_length=64)
        schema.add_field("assistant_message_id", DataType.VARCHAR, max_length=64)
        schema.add_field("conversation_time", DataType.INT64)
        schema.add_field("status", DataType.VARCHAR, max_length=20)
        schema.add_field(
            "dense_vector",
            DataType.FLOAT_VECTOR,
            dim=milvus_config.embedding_dim,
        )
        schema.add_field(
            "sparse_vector",
            datatype=DataType.SPARSE_FLOAT_VECTOR,
        )

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="meeting_id",
            index_name="conversation_meeting_id_index",
            index_type="INVERTED",
        )
        index_params.add_index(
            field_name="status",
            index_name="conversation_status_index",
            index_type="INVERTED",
        )
        index_params.add_index(
            field_name="dense_vector",
            index_name="conversation_dense_vector_index",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 32, "efConstruction": 300},
        )
        index_params.add_index(
            field_name="sparse_vector",
            index_type="SPARSE_INVERTED_INDEX",
            index_name="conversation_sparse_vector_index",
            metric_type="IP",
            params={"inverted_index_algo": "DAAT_MAXSCORE"},
        )
        await client.create_collection(
            collection_name=collection_name,
            schema=schema,
            index_params=index_params,
        )
        _conversation_memory_initialized = True
        logger.info(f"L3历史对话集合创建成功：{collection_name}")


async def initialize_office_task_collection() -> None:
    """应用启动时创建L4办公任务集合和稠密向量索引和稀疏向量索引。"""

    global _init_lock, _office_task_initialized

    if _office_task_initialized:
        return
    if _init_lock is None:
        _init_lock = asyncio.Lock()

    collection_name = milvus_config.office_task_collection
    if not collection_name:
        raise ValueError("缺少OFFICE_TASK_COLLECTION环境变量配置")

    async with _init_lock:
        if _office_task_initialized:
            return

        client = get_async_milvus_client()
        if await client.has_collection(collection_name=collection_name):
            description = await client.describe_collection(
                collection_name=collection_name
            )
            field_names = {
                field.get("name") or field.get("field_name")
                for field in description.get("fields", [])
                if isinstance(field, dict)
            }
            expected_fields = {
                "task_id",
                "meeting_id",
                "title",
                "dense_vector",
                "sparse_vector",
            }
            if not expected_fields.issubset(field_names):
                raise RuntimeError(
                    "L4办公任务集合Schema不兼容，请迁移或重建集合："
                    f"{collection_name}"
                )
            _office_task_initialized = True
            logger.info(f"L4办公任务集合已初始化：{collection_name}")
            return

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("task_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("meeting_id", DataType.VARCHAR, max_length=64)
        schema.add_field("title", DataType.VARCHAR, max_length=1024)
        schema.add_field(
            "dense_vector",
            DataType.FLOAT_VECTOR,
            dim=milvus_config.embedding_dim,
        )
        schema.add_field(
            "sparse_vector",
            datatype=DataType.SPARSE_FLOAT_VECTOR,
        )

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="meeting_id",
            index_name="office_task_meeting_id_index",
            index_type="INVERTED",
        )
        index_params.add_index(
            field_name="dense_vector",
            index_name="office_task_dense_vector_index",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 32, "efConstruction": 300},
        )
        index_params.add_index(
            field_name="sparse_vector",
            index_type="SPARSE_INVERTED_INDEX",
            index_name="office_task_sparse_vector_index",
            metric_type="IP",
            params={"inverted_index_algo": "DAAT_MAXSCORE"},
        )
        await client.create_collection(
            collection_name=collection_name,
            schema=schema,
            index_params=index_params,
        )
        _office_task_initialized = True
        logger.info(f"L4办公任务集合创建成功：{collection_name}")

async def async_milvus_health_check() -> bool:
    try:
        await get_async_milvus_client().list_collections()
        return True
    except Exception as exc:
        logger.warning(f"Milvus异步健康检查失败，原因：{exc}")
        return False


async def close_async_milvus_client() -> None:
    global _async_milvus_client
    global _init_lock, _conversation_memory_initialized, _office_task_initialized
    client = _async_milvus_client
    _async_milvus_client = None
    _init_lock = None
    _conversation_memory_initialized = False
    _office_task_initialized = False
    if client is not None:
        await client.close()


def _coerce_int64_ids(ids):
    """
    转换chunk_id为Milvus要求的INT64类型（主键字段schema为INT64）
    过滤无效ID，分离可转换/不可转换的ID
    :param ids: 待转换的chunk_id列表
    :return: 元组(ok_ids, bad_ids)，ok_ids为可转换的int64类型ID列表，bad_ids为无效ID列表
    """
    ok, bad = [], []
    for x in (ids or []):
        if x is None:
            continue
        try:
            ok.append(int(x))
        except Exception:
            bad.append(x)
    return ok, bad


def fetch_chunks_by_chunk_ids(
        client,
        collection_name: str,
        chunk_ids,
        *,
        output_fields=None,
        batch_size: int = 100,
):
    """
    通过chunk_id主键批量查询Milvus中的切片数据
    用于补全「仅拥有chunk_id无文本内容」场景的切片信息
    优先使用get方法（主键直查，性能最优），失败则回退query过滤查询
    :param client: MilvusClient实例
    :param collection_name: 集合名称
    :param chunk_ids: 待查询的chunk_id列表
    :param output_fields: 需要返回的字段列表，默认返回核心切片字段
    :param batch_size: 分批查询大小，避免单次查询数据量过大，默认100
    :return: List[dict]，Milvus实体字典列表，查询失败返回空列表
    """
    # 前置校验：客户端/集合名无效直接返回空
    if client is None:
        return []
    if not collection_name:
        return []
    # 默认返回字段：核心切片标识与内容字段
    if output_fields is None:
        output_fields = ["chunk_id", "content", "title", "parent_title", "item_name"]

    # 转换ID为INT64类型，分离有效/无效ID
    ok_ids, bad_ids = _coerce_int64_ids(chunk_ids)
    if bad_ids:
        # 记录无效ID，跳过查询
        logger.warning(f"存在无法转换为INT64的chunk_id，将跳过查询：{bad_ids}")

    # 无有效ID直接返回空
    if not ok_ids:
        return []

    results = []
    # 分批查询：按batch_size切分有效ID，循环查询
    for i in range(0, len(ok_ids), batch_size):
        batch = ok_ids[i: i + batch_size]

        # 方式1：优先使用主键get方法查询（性能最优）
        if hasattr(client, "get"):
            try:
                got = client.get(collection_name=collection_name, ids=batch, output_fields=output_fields)
                if got:
                    results.extend(got)
                continue
            except Exception as e:
                logger.warning(f"Milvus get方法查询失败，将回退至query方法：{str(e)}")

        # 方式2：get方法失败，回退使用filter过滤查询
        try:
            expr = f"chunk_id in [{', '.join(str(x) for x in batch)}]"
            q = client.query(collection_name=collection_name, filter=expr, output_fields=output_fields)
            if q:
                results.extend(q)
        except Exception as e:
            logger.error(f"Milvus query方法批量查询chunk_id失败：{str(e)}", exc_info=True)

    return results


def create_hybrid_search_requests(dense_vector, sparse_vector, dense_params=None, sparse_params=None, expr=None,
                                  limit=5):
    """
    构建Milvus混合搜索请求对象
    分别创建稠密/稀疏向量的搜索请求，用于后续混合搜索融合
    :param dense_vector: 文本生成的稠密向量
    :param sparse_vector: 文本生成的稀疏向量
    :param dense_params: 稠密向量搜索参数，默认使用余弦相似度
    :param sparse_params: 稀疏向量搜索参数，默认使用内积相似度
    :param expr: 搜索过滤表达式，用于精准筛选数据
    :param limit: 单向量搜索返回结果数量，默认5
    :return: 搜索请求列表，包含[dense_req, sparse_req]
    """
    # 稠密向量默认搜索参数：余弦相似度（COSINE），适配BGE-M3稠密向量并与建库参数保持一致
    if dense_params is None:
        dense_params = {"metric_type": "COSINE"}
    # 稀疏向量默认搜索参数：内积（IP），适配BGE-M3稀疏向量
    if sparse_params is None:
        sparse_params = {"metric_type": "IP"}

    # 构建稠密向量搜索请求，关联Milvus的dense_vector字段 近似最近邻（ANN）检索请求的核心类
    dense_req = AnnSearchRequest(
        data=[dense_vector],
        anns_field="dense_vector",
        param=dense_params,
        expr=expr,
        limit=limit
    )

    # 构建稀疏向量搜索请求，关联Milvus的sparse_vector字段
    sparse_req = AnnSearchRequest(
        data=[sparse_vector],
        anns_field="sparse_vector",
        param=sparse_params,
        expr=expr,
        limit=limit
    )

    return [dense_req, sparse_req]


async def async_hybrid_search(
    client: AsyncMilvusClient,
    collection_name: str,
    reqs: list[AnnSearchRequest],
    ranker_weights: tuple[float, float] = (0.8, 0.2),
    norm_score: bool = True,
    limit: int = 5,
    output_fields: list[str] | None = None,
):
    """使用异步客户端融合稠密、稀疏两路检索结果。"""

    ranker = WeightedRanker(
        ranker_weights[0],
        ranker_weights[1],
        norm_score=norm_score,
    )
    return await client.hybrid_search(
        collection_name=collection_name,
        reqs=reqs,
        ranker=ranker,
        limit=limit,
        output_fields=output_fields,
    )


def hybrid_search(client, collection_name, reqs, ranker_weights=(0.5, 0.5), norm_score=False, limit=5,
                  output_fields=None, search_params=None):
    """
    执行Milvus稠密+稀疏向量混合搜索
    基于WeightedRanker实现双向量搜索结果加权融合，提升检索准确性
    :param client: MilvusClient实例
    :param collection_name: 集合名称
    :param reqs: 搜索请求列表，固定为[dense_req, sparse_req]
    :param ranker_weights: 加权融合权重，默认(0.5,0.5)，依次对应稠密/稀疏向量
    :param norm_score: 是否归一化评分后再融合，避免评分量级差异导致权重失效
    :param limit: 混合搜索最终返回结果数量，默认5
    :param output_fields: 需要返回的字段列表，默认返回item_name
    :param search_params: 搜索参数，如ef/topk等，默认None
    :return: 混合搜索结果列表，搜索失败返回None
    """
    try:
        # 初始化加权排名器：按权重融合稠密/稀疏向量的搜索结果
        # norm_score=True：先将两个向量评分归一化到0~1区间，再加权计算
        rerank = WeightedRanker(ranker_weights[0], ranker_weights[1], norm_score=norm_score)

        # 默认返回字段：文档标识字段
        if output_fields is None:
            output_fields = ["item_name"]

        # 执行混合搜索：融合稠密+稀疏向量结果，按权重重新排序
        res = client.hybrid_search(
            collection_name=collection_name,
            reqs=reqs,
            ranker=rerank,
            limit=limit,
            output_fields=output_fields,
            search_params=search_params
        )

        logger.info(f"Milvus混合搜索完成，集合[{collection_name}]共检索到{len(res[0])}条结果")
        return res
    except Exception as e:
        logger.error(f"Milvus混合搜索执行失败，集合[{collection_name}]：{str(e)}", exc_info=True)
        return None
