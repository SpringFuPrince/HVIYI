from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.clients.milvus_utils import (
    close_async_milvus_client,
    initialize_conversation_memory_collection,
)
from app.clients.mongo_utils import close_mongo_memory_client, get_mongo_memory_client
from app.clients.mysql_utils import close_mysql_client, get_mysql_client
from app.clients.redis_utils import (
    close_redis_memory_cache,
    initialize_redis_memory_cache,
)
from app.db.base import Base
from app.graph.import_process.api.import_service import router as import_router
from app.graph.query_graph.api.query_server import router as query_router
from app.api.server import router as local_router
from app.utils.logger import logger


#  uv run uvicorn app.api.main:app --host 127.0.0.1 --port 8000

@asynccontextmanager
async def lifespan(_: FastAPI):
    """统一初始化三个 API 模块共用的数据库与向量服务。"""

    await get_mysql_client().create_all(Base.metadata)
    await initialize_redis_memory_cache()
    try:
        await get_mongo_memory_client().initialize_indexes()
    except Exception as exc:
        logger.exception(f"L2会议记忆索引初始化失败，服务将降级运行：{exc}")
    try:
        await initialize_conversation_memory_collection()
    except Exception as exc:
        logger.exception(f"L3历史对话集合初始化失败，服务将降级运行：{exc}")

    try:
        yield
    finally:
        await close_redis_memory_cache()
        await close_mongo_memory_client()
        await close_async_milvus_client()
        await close_mysql_client()


app = FastAPI(
    title="HVIYI Meeting Agent",
    description="本地单用户会议、Import Graph、Query Graph 与实时 ASR 统一服务",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(import_router, prefix="/api/import", tags=["Import Graph"])
app.include_router(query_router, prefix="/api/query", tags=["Query Graph"])
app.include_router(local_router, prefix="/api/local", tags=["Local Meeting"])


@app.get("/health", tags=["System"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
