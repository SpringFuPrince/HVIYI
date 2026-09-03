from typing import TypedDict, Literal
import copy

from app.utils.logger import logger, PROJECT_ROOT


class ImportGraphState(TypedDict, total=False):
    # ===== 任务和数据范围 =====
    task_id: str                # 任务唯一ID，用于追踪日志
    meeting_id: str             # 会议ID
    document_id: str            # 文档ID

    # ===== 输入文件 =====
    file_type: Literal["pdf", "pptx", "docx", "md", "transcript_md",""]
    file_title: str             # 文件标题（文件名去后缀）
    is_transcript: bool         # 是否是转录文件

    input_doc_path: str           # 原始输入文件路径（格式转换前）
    output_local_dir: str         #  输出目录
    # content_sha256: str        # 内容的 SHA256 哈希值，用于唯一标识

    # ===== md文档 =====
    md_path: str                # Markdown 文件路径 (转换后或直接输入的)（最终要切分的文件路径）
    md_content: str             # Markdown 的全文内容
    transcript: list[dict]      # 转录切片元数据 [{speaker, ts}]（由会议实时转写随 md 传入

    # ===== 文档处理结果 =====
    chunks: list[dict]          # 切片后的文本列表

    # ===== 会议内容提取结果（当处理会议语音时才会有值，并需存memory）=====
    meeting_summary: str       # 会议摘要总结
    # minutes: list[dict]       # 会议分钟列表 [{speaker, ts, content}]
    new_office_task: list[dict]  # 新提出的工作任务列表

    # ===== 数据库存储结果 =====
    original_doc_path: str    # 原始 PDF/PPT/Word/Markdown 在 MinIO 中的位置
    # normalized_object_key: str  # 转换后的标准 Markdown 在 MinIO 中的位置 (待定，转换后的存本地即可)
    # indexed_count: int          # 已索引的切片数量（待定）

    # ===== 异常信息 =====
    error: str

graph_default_state: ImportGraphState = {
    "task_id":"",
    "meeting_id":"",
    "document_id":"",
    "file_type":"",
    "file_title":"",
    "is_transcript":False,
    "input_doc_path":"",
    "output_local_dir":"",
    # "content_sha256":"",
    "md_path":"",
    "md_content":"",
    "transcript":[],
    "chunks":[],
    "meeting_summary":"",
    # "minutes":[],
    "new_office_task":[],
    "original_doc_path":"",
    "error":"",
}

def create_default_state(**overrides) -> ImportGraphState:
    """
    创建默认状态，支持覆盖

    Args:
        **overrides: 要覆盖的字段（关键字参数解包）

    Returns:
        新的状态实例

    Examples:
        state = create_default_state(task_id="task_001", local_file_path="doc.pdf")
    """

    # 默认状态
    state = copy.deepcopy(graph_default_state)
    # 用 overrides 覆盖默认值
    state.update(overrides)
    # 返回创建好的状态字典实例
    return state


def get_default_state() -> ImportGraphState:
    """
    返回一个新的状态实例，避免全局变量污染
    """
    return copy.deepcopy(graph_default_state)




if __name__ == "__main__":
    """
    测试
    """
    # 创建默认状态
    state = create_default_state(local_file_path="万用表RS-12的使用.pdf")
    logger.info(state)
