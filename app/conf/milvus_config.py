# 导入核心依赖（和其他配置类共用，只需导入一次）
from dataclasses import dataclass
import os
from dotenv import load_dotenv


load_dotenv()

# 定义Milvus向量数据库配置类
@dataclass
class MilvusConfig:
    milvus_url: str | None          # Milvus服务端连接地址
    milvus_token: str | None
    chunks_collection: str | None   # 存储切片的集合名称
    doc_meta_collection: str | None  # 存储文档元数据的集合名称
    conversation_memory_collection: str | None  # 存储会话记忆的集合名称
    embedding_dim: int


milvus_config = MilvusConfig(
    milvus_url=os.getenv("MILVUS_URL"),
    milvus_token=os.getenv("MILVUS_TOKEN"),
    chunks_collection=os.getenv("CHUNKS_COLLECTION"),
    doc_meta_collection=os.getenv("DOC_META_COLLECTION"),
    conversation_memory_collection=os.getenv( "CONVERSATION_MEMORY_COLLECTION" ),
    embedding_dim=int(os.getenv("EMBEDDING_DIM", "1024")),
)
