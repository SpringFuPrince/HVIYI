import json
import threading
from typing import Any
from pydantic import BaseModel, Field
from app.conf.lm_config import lm_config
from app.memory.memory_manager import get_memory_manager
from app.utils.load_prompt import load_prompt
from app.lm.llm_utils import get_llm_client
from app.memory.models import MemoryRecallRequest, MemoryResponse

"""
1. 调用 MemoryManager 召回 L1～L4
2. 对 L1 完整会话和 L3 历史问答去重
3. 将每层数据转换成适合大模型阅读的文本
4. 每个模块分别判断是否压缩
5. 加入 RAG 最终文档，拼成 context_text
"""


class ContextLimits(BaseModel):
    """每个上下文模块独立使用的字符预算。"""

    conversation_chars: int = Field(default=5000, gt=0, description="L1会话模块字符预算")
    meeting_chars: int = Field(default=1000, gt=0, description="L2会议总结模块字符预算")
    office_progress_chars: int = Field(default=2000, gt=0, description="L4办公进度模块字符预算")
    semantic_chars: int = Field(default=1000, gt=0, description="L3语义检索模块字符预算")
    rag_chars: int = Field(default=8000, gt=0, description="RAG模块字符预算")
    recent_messages_to_keep: int = Field(default=6, ge=1, description="L1保留最近消息数")
    max_compression_rounds: int = Field(default=3, ge=1, le=5, description="L1最大压缩轮数")


class ContextCompressionResult(BaseModel):
    """压缩模型的结构化输出。"""
    compressed_text: str = Field(description="压缩后保留关键信息的文本")

class ContextBuildResult(BaseModel):
    """ContextManager的返回值。"""
    context_text: str = ""
    memory: MemoryResponse = Field(default_factory=MemoryResponse)


class ContextManager:

    def __init__(
        self,
        limits: ContextLimits | None = None,
    ):
        self._memory_manager = get_memory_manager()
        self._compression_llm = get_llm_client(lm_config.compression_model)
        self._structured_compressor = None
        self._limits = limits or ContextLimits()

    async def build(
        self,
        request: MemoryRecallRequest,
        rag_documents: list[dict[str, Any]] | None = None,
    ) -> ContextBuildResult:
        """
        构建上下文
        四层记忆+RAG+prompt
        """

        # 召回记忆
        memory = await self._memory_manager.recall(request)
        # 去重L1和L3
        memory = self._quchong_l1_l3(memory, request.scope.session_id)

        # 保存最终的上下文
        sections: list[tuple[str, str]] = []

        # 用户画像全部加入上下文
        facts_text = self._to_json(memory.facts) if memory.facts else ""
        self._append_section(sections, "本机用户长期画像", facts_text)

        # 处理 L1 滚动摘要+最近消息
        conversation_text = await self._prepare_conversation(request.scope,memory)
        self._append_section(sections, "当前会话完整对话", conversation_text)

        # 处理 L2 会议情节记忆
        meeting_text = self._format_meeting(memory.meeting_episode)
        # 压缩
        meeting_text = await self._compress_message(
            module="meeting_episode",
            text=meeting_text,
            max_chars=self._limits.meeting_chars,
            focus="保留会议总结、决定、任务、负责人、截止时间、里程碑和风险。",
        )
        self._append_section(sections, "当前会议情节记忆", meeting_text)

        # L4 办公任务处理结果
        office_operation_text = (
            self._to_json(memory.office_operation)
            if memory.office_operation
            else ""
        )
        self._append_section(sections,"办公任务处理结果",office_operation_text)

        # 处理 L4 办公进度
        office_text = self._format_office_progress(memory.office_progress)
        # 压缩
        office_text = await self._compress_message(
            module="office_progress",
            text=office_text,
            max_chars=self._limits.office_progress_chars,
            focus="保留任务标题、负责人、状态、进度、截止时间和未完成事项。",
        )
        self._append_section(sections, "办公任务进度", office_text)

        # 处理 L3 语义检索
        semantic_text = self._format_semantic_conversations( memory.semantic_conversations )
        # 压缩
        semantic_text = await self._compress_message(
            module="semantic_conversations",
            text=semantic_text,
            max_chars=self._limits.semantic_chars,
            focus="保留与当前问题相关的历史结论、约束、问题和回答。",
        )
        self._append_section(sections, "相关历史问答", semantic_text)

        # 处理 RAG文档
        rag_text = self._format_rag_documents(rag_documents or [])
        # 压缩
        rag_text = await self._compress_message(
            module="rag_documents",
            text=rag_text,
            max_chars=self._limits.rag_chars,
            focus=(
                "保留回答问题需要的原文证据、数字、步骤、限制条件，"
                "并保留每条证据的文档标题、来源、URL或chunk_id。"
            ),
        )
        self._append_section(sections, "RAG检索文档", rag_text)


        context_text = ""
        if sections:
            context_text = "\n\n".join( f"## {title}\n{text}" for title, text in sections )

        return ContextBuildResult(context_text=context_text,memory=memory )


    async def _prepare_conversation(self, scope, memory: MemoryResponse ) -> str:
        """
        滚动摘要构造 L1 上下文。
        """
        # 最近几条消息
        messages = memory.session_messages
        # 滚动摘要
        old_summary = memory.session_summary.strip()
        messages_text = self._format_messages(messages)

        full_text = self._combine_summary_and_messages(old_summary, messages_text)
        if not full_text:
            return ""

        # 获取L1上下文预算
        limit = self._limits.conversation_chars
        if len(full_text) <= limit:
            return full_text
        # 防止截断长度超过messages的长度
        keep_count = min( self._limits.recent_messages_to_keep,len(messages) )

        older_messages = messages[:-keep_count]
        recent_messages = messages[-keep_count:]
        older_text = self._format_messages(older_messages)
        recent_text = self._format_messages(recent_messages)

        summary_limit = limit - len(recent_text)

        # 最近M条原文已经占满预算，或者根本没有可单独压缩的早期内容时，
        # 把旧摘要和全部新增消息一起压缩，through_sequence_id指向最后一条消息。
        if summary_limit <= 0 or not (old_summary or older_text):
            summary = await self._compress_message(
                module="session_conversation_all_messages",
                text=full_text,
                max_chars=limit,
                focus=(
                    """
                    压缩当前完整会话，按时间顺序保留用户目标、指代对象,
                    重要事实、决定、承诺、偏好、最近对话进展和未解决问题
                    """
                ),
                force=True, # 强制压缩
            )
            # 滚动摘要覆盖的最后一条消息的sequence_id
            through_sequence_id = self._last_sequence_id(
                messages,
                memory.session_summarized_through_sequence_id,
            )
            await self._save_rolling_summary(
                scope=scope,
                memory=memory,
                summary=summary,
                through_sequence_id=through_sequence_id,
                remaining_messages=[],
            )
            return summary

        # 压缩输入只包含旧摘要和本次新增的较早消息，不再查询全部历史原文。
        summary_input = self._combine_summary_and_messages(old_summary, older_text)
        summary = await self._compress_message(
            module="session_conversation_older_messages",
            text=summary_input,
            max_chars=summary_limit,
            focus=(
                "按时间顺序压缩较早对话，保留用户目标、指代对象、"
                "重要事实、决定、承诺、偏好和仍未解决的问题。"
            ),
            # 整体已经超限，因此即使输入本身较短也要按剩余预算重写。
            force=True,
        )
        through_sequence_id = self._last_sequence_id(
            older_messages,
            memory.session_summarized_through_sequence_id,
        )
        await self._save_rolling_summary(
            scope=scope,
            memory=memory,
            summary=summary,
            through_sequence_id=through_sequence_id,
            remaining_messages=recent_messages,
        )
        final_text = summary + "\n\n" + recent_text
        return final_text

    async def _save_rolling_summary(self,
        scope,
        memory: MemoryResponse,
        summary: str,
        through_sequence_id: int,
        remaining_messages: list[dict],
    ) -> None:
        """
        保存滚动摘要
        """

        saved = await self._memory_manager.save_short_term_summary(
            scope=scope,
            summary=summary,
            summarized_through_sequence_id=through_sequence_id,
            expected_version=memory.session_summary_version,
        )
        if not saved:
            memory.warnings.append( "L1滚动摘要发生并发更新，本次上下文使用临时压缩结果" )
            return

        memory.session_summary = summary
        memory.session_summary_version += 1
        memory.session_summarized_through_sequence_id = through_sequence_id
        memory.session_messages = remaining_messages

    @staticmethod
    def _combine_summary_and_messages(summary: str, messages_text: str) -> str:
        """
        合并滚动摘要和最近消息
        """
        parts = [part for part in (summary, messages_text) if part]
        return "\n\n".join(parts)

    @staticmethod
    def _last_sequence_id(messages: list[dict], fallback: int) -> int:
        if not messages:
            return fallback
        return int(messages[-1]["sequence_id"])

    async def _compress_message(self,module: str,text: str,max_chars: int,focus: str,force: bool = False) -> str:
        """
        某模块超预算时，调用llm循环压缩
        params:
            module: 模块名称
            text: 待压缩文本
            max_chars: 最大压缩字符数
            focus: 压缩焦点提示词
            force: 是否强制压缩
        return:
            压缩后的文本
        """

        # 空直接返回
        if not text:
            return ""
        # 没超预算或不强制压缩，直接返回
        if not force and len(text) <= max_chars:
            return text

        current_text = text
        compressor = self._get_structured_compressor()
        prompt = load_prompt("context_compression")

        # 循环压缩
        for round_number in range(1,self._limits.max_compression_rounds + 1):
            message = json.dumps(
                {
                    "module": module,
                    "focus": focus,
                    "max_chars": max_chars,
                    "content": current_text,
                },
                ensure_ascii=False,
                default=str,
            )
            response = await compressor.ainvoke( [("system", prompt), ("human", message)] )
            result = ContextCompressionResult.model_validate(response)
            compressed_text = result.compressed_text.strip()
            if not compressed_text:
                raise RuntimeError(f"{module}压缩模型返回了空内容")

            current_text = compressed_text
            if len(current_text) <= max_chars:
                return current_text

        raise RuntimeError( f"{module}经过{self._limits.max_compression_rounds}轮压缩后仍超过{max_chars}字符" )

    def _get_structured_compressor(self):
        """
        获取结构化文本压缩模型
        """
        if self._structured_compressor is not None:
            return self._structured_compressor

        llm = self._compression_llm or get_llm_client(
            model=lm_config.compression_model
        )
        self._structured_compressor = llm.with_structured_output(
            ContextCompressionResult,
            method="json_mode",
        )
        return self._structured_compressor

    def _quchong_l1_l3(self,memory: MemoryResponse,current_session_id: str | None ):
        """
        L1/L3 去重
        """
        # 深拷贝
        result = memory.model_copy(deep=True)

        session_message_ids = set()
        for message in result.session_messages:
            message_id = message.get("message_id")
            if message_id:
                session_message_ids.add(str(message_id))
        # 去重后的L3
        unique_conversations: list[dict[str, Any]] = []
        # 重复的L3会话
        seen_pairs: set[tuple[str, str]] = set()

        for conversation in result.semantic_conversations:
            #
            same_session = bool(current_session_id and (conversation.get("session_id") or "") == current_session_id)

            user_message = conversation.get("user_message") or {}
            assistant_message = conversation.get("assistant_message") or {}
            message_pair = (
                str(user_message.get("message_id") or ""),
                str(assistant_message.get("message_id") or ""),
            )
            overlaps_l1 = any(
                message_id and message_id in session_message_ids for message_id in message_pair
            )
            repeated_l3 = any(message_pair) and message_pair in seen_pairs
            if same_session or overlaps_l1 or repeated_l3:
                continue

            if any(message_pair):
                seen_pairs.add(message_pair)
            unique_conversations.append(conversation)

        result.semantic_conversations = unique_conversations
        return result

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        """
        格式化会话消息为字符串
        """
        role_names = {
            "user": "用户",
            "assistant": "助手",
            "system": "系统",
            "tool": "工具",
        }
        items = []
        for message in messages:
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            role = str(message.get("role") or "unknown")
            items.append(f"{role_names.get(role, role)}：{content}")
        return "\n\n".join(items)

    @classmethod
    def _format_meeting(cls, meeting: dict | None) -> str:
        """
        格式化会议情节记忆为字符串
        """
        if not meeting:
            return ""
        if "meeting_data" in meeting:
            payload = {
                "meeting_id": meeting.get("meeting_id"),
                "meeting_data": meeting.get("meeting_data"),
            }
        else:
            payload = {
                key: value
                for key, value in meeting.items()
                if key != "_id"
            }
        return cls._to_json(payload)

    @classmethod
    def _format_office_progress(cls, tasks: list[dict]) -> str:
        """
        格式化办公进度为字符串
        """
        fields = (
            "task_id",
            "meeting_id",
            "title",
            "description",
            "owner",
            "status",
            "progress",
            "due_at",
        )
        items = []
        for task in tasks:
            payload = {
                field: task.get(field)
                for field in fields
                if task.get(field) is not None
            }
            if payload:
                items.append(cls._to_json(payload))
        return "\n\n".join(items)

    @staticmethod
    def _format_semantic_conversations(conversations: list[dict]) -> str:
        """
        格式化语义检索结果为字符串。
        """
        items = []
        for conversation in conversations:
            user_message = conversation.get("user_message") or {}
            assistant_message = conversation.get("assistant_message") or {}
            user_content = str(user_message.get("content") or "").strip()
            assistant_content = str(
                assistant_message.get("content") or ""
            ).strip()
            if not user_content and not assistant_content:
                continue

            lines = []
            score = conversation.get("score")
            if isinstance(score, (int, float)):
                lines.append(f"相关度：{score:.4f}")
            if user_content:
                lines.append(f"用户：{user_content}")
            if assistant_content:
                lines.append(f"助手：{assistant_content}")
            items.append("\n".join(lines))
        return "\n\n".join(items)

    @classmethod
    def _format_rag_documents(cls, documents: list[dict[str, Any]]) -> str:
        """
        格式化RAG文档为字符串。
        """
        items = []
        for index, document in enumerate(documents, start=1):
            metadata = {
                key: document.get(key)
                for key in (
                    "source",
                    "title",
                    "url",
                    "chunk_id",
                    "score",
                )
                if document.get(key) is not None
            }
            text = str(
                document.get("text")
                or document.get("content")
                or ""
            ).strip()
            if not text:
                continue
            items.append(
                f"文档{index}：{cls._to_json(metadata)}\n正文：{text}"
            )
        return "\n\n".join(items)

    @staticmethod
    def _append_section(sections: list[tuple[str, str]],title: str,text: str) -> None:
        if text:
            sections.append((title, text))


    @staticmethod
    def _to_json(value: Any) -> str:
        """
        将任意Python对象转换为JSON字符串。
        """
        return json.dumps(
            value,
            ensure_ascii=False,
            default=str,  # 处理非JSON可序列化对象如datetime
        )

_context_manager: ContextManager | None = None
_context_manager_lock = threading.Lock()


def get_context_manager() -> ContextManager:
    """懒加载进程级 ContextManager 单例，使用默认 ContextLimits。"""
    global _context_manager

    if _context_manager is None:
        with _context_manager_lock:
            if _context_manager is None:
                _context_manager = ContextManager()

    return _context_manager
