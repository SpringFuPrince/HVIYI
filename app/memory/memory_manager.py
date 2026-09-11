import threading
from typing import Any
from app.utils.logger import logger
from app.memory.models import MemoryLayer,MemoryRecallRequest,MemoryResponse,MemoryScope,MemoryWriteRequest
from app.clients.mysql_utils import get_mysql_client
from app.clients.mongo_utils import get_mongo_memory_client
from app.clients.milvus_utils import get_async_milvus_client

from app.memory.short_term_memory import ShortTermMemory
from app.memory.episodic_memory import EpisodicMemory
from app.memory.semantic_memory import SemanticMemory
from app.memory.progress_memory import OfficeProgressMemory



class MemoryManager:
    def __init__(
        self,
        short_term_memory,
        episodic_memory,
        semantic_memory,
        office_progress_memory,
    ):
        """
        MemoryManager按layer路由到对应的记忆层
        """
        self._layers: dict[MemoryLayer, Any] = {
            MemoryLayer.SHORT_TERM: short_term_memory,
            MemoryLayer.EPISODIC: episodic_memory,
            MemoryLayer.SEMANTIC: semantic_memory,
            MemoryLayer.OFFICE_PROGRESS: office_progress_memory,
        }

    async def write(self, request: MemoryWriteRequest):
        """
        统一入口
        写入一条记忆
        Manager根据layer找到对应Memory
        """
        if not request.scope.meeting_id:
            raise ValueError("缺少meeting_id")

        # 根据layer找到对应Memory实例
        memory_object = self._get_memory_object(request.layer)
        # 调用对应Memory实例的write方法
        result = await memory_object.write(request)
        return result


    async def write_many(self, requests: list[MemoryWriteRequest]) -> list:
        """
        顺序写入多条记忆。

        不同层使用不同数据库，
        因此无法实现跨层数据库事务。
        """
        results = []

        for request in requests:
            result = await self.write(request)
            results.append(result)

        return results



    async def recall(self, request: MemoryRecallRequest) -> MemoryResponse:
        """
        查询指定层的记忆，并组装统一MemoryResponse。

        每一个Memory的recall()都返回一个
        只填充本层字段的MemoryResponse。
        """
        if not request.scope.meeting_id:
            raise ValueError("缺少meeting_id")

        response = MemoryResponse()

        selected_layers = self._get_recall_layers(request.layers)

        for layer in selected_layers:
            memory_object = self._get_memory_object(layer)

            try:
                # 调用对应Memory实例的recall方法
                partial_response = await memory_object.recall(request)
                # 检查recall()返回的是否为MemoryResponse，后续需合并四层记忆
                if not isinstance(partial_response, MemoryResponse):
                    raise TypeError(f"{layer.value}.recall()返回的不是MemoryResponse")
                # 合并当前层的记忆到总响应
                self._merge_response(response, partial_response)

            except Exception as exc:
                logger.exception(f"{layer.value}记忆召回失败")
                # 某层查询失败，不影响其他层继续召回
                response.warnings.append(f"{layer.value}记忆召回失败：{exc}")

        return response

    async def save_short_term_summary(self,scope: MemoryScope,summary: str,summarized_through_sequence_id: int,expected_version: int) -> bool:
        """
        把 L1 滚动摘要写入ShortTermMemory
        """

        if not scope.meeting_id:
            raise ValueError("缺少meeting_id")

        memory_object = self._get_memory_object(MemoryLayer.SHORT_TERM)
        return await memory_object.save_session_summary(
            scope=scope,
            summary=summary,
            summarized_through_sequence_id=summarized_through_sequence_id,
            expected_version=expected_version,
        )

    async def get_recent_messages(self,scope: MemoryScope,limit: int = 10) -> list[dict]:
        """
        供问题重写节点直接读取最近 K 条消息。
        """

        if not scope.meeting_id:
            raise ValueError("缺少meeting_id")

        if limit <= 0:
            raise ValueError("limit 必须大于 0")

        short_term_memory = self._get_memory_object(MemoryLayer.SHORT_TERM)

        return await short_term_memory.get_recent_messages(scope,limit)


    def _get_memory_object(self, layer: MemoryLayer):
        """
        获取当前Request对应的Memory实例。
        """
        memory_object = self._layers.get(layer)

        if memory_object is None:
            raise ValueError(
                f"不支持的记忆层级：{layer}"
            )

        return memory_object



    def _get_recall_layers(self,requested_layers: set[MemoryLayer]) -> list[MemoryLayer]:
        """
        决定查询哪些层的记忆。
        没有指定layers，查询全部四层记忆。
        返回：
        [
            MemoryLayer.SHORT_TERM,
            MemoryLayer.EPISODIC,
            MemoryLayer.SEMANTIC,
            MemoryLayer.OFFICE_PROGRESS,
        ]
        """
        if not requested_layers:
            return list(self._layers.keys())

        # 保证顺序
        return [ layer for layer in self._layers if layer in requested_layers ]


    def _merge_response(
        self,
        target: MemoryResponse, # 最终返回的response
        source: MemoryResponse, # 各层召回的response
    ):
        """
        把某一层返回的局部响应合并到总响应。
        """
        # L1 未摘要的原始消息
        target.session_messages.extend(source.session_messages)
        # L1 滚动摘要
        if (
            source.session_summary
            or source.session_summary_version
            or source.session_summarized_through_sequence_id
        ):
            target.session_summary = source.session_summary
            target.session_summary_version = source.session_summary_version
            target.session_summarized_through_sequence_id = (
                source.session_summarized_through_sequence_id
            )
        
        # L2 会议情节记忆
        if source.meeting_episode is not None:
            target.meeting_episode = source.meeting_episode

        # L2 用户画像
        target.facts.update(source.facts)
        
        # L3 语义对话
        target.semantic_conversations.extend(
            source.semantic_conversations
        )
        
        # L4 办公进度
        target.office_progress.extend(
            source.office_progress
        )

        # L4 办公操作的执行状态
        if source.office_operation is not None:
            target.office_operation = source.office_operation
        
        # 汇总各层警告信息
        target.warnings.extend(
            source.warnings
        )

_memory_manager: MemoryManager | None = None
_memory_manager_lock = threading.Lock()


def get_memory_manager() -> MemoryManager:
    global _memory_manager

    if _memory_manager is None:
        with _memory_manager_lock:
            if _memory_manager is None:
                mysql_client = get_mysql_client()
                mongo_client = get_mongo_memory_client()
                milvus_client = get_async_milvus_client()

                _memory_manager = MemoryManager(
                    short_term_memory=ShortTermMemory(mysql_client),
                    episodic_memory=EpisodicMemory(mongo_client),
                    semantic_memory=SemanticMemory(
                        mysql_client=mysql_client,
                        milvus_client=milvus_client,
                    ),
                    office_progress_memory=OfficeProgressMemory(
                        mysql_client=mysql_client,
                        milvus_client=milvus_client,
                    ),
                )

    return _memory_manager
