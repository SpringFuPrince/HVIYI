import sys
from pathlib import Path
from typing import Literal
from app.clients.minio_utils import upload_document
from app.graph.import_process.agent.state import ImportGraphState
from app.utils.logger import logger
from app.utils.path_util import PROJECT_ROOT
from app.utils.task_utils import add_done_task, add_running_task


def _required_state_value(state: ImportGraphState, field_name: str) -> str:
    # 安全获取状态值
    value = state.get(field_name) or ""
    if not value:
        raise ValueError(f"入口节点缺少 {field_name}")
    return value


def detect_file_type(file_path: Path, state:ImportGraphState):
    """
    检测文件类型
    用于LangGraph条件边路由
    """
    suffix = file_path.suffix.lower()

    if state["is_transcript"]:
        return "transcript_md"
    if suffix in [".md", ".pdf", ".pptx", ".docx"]:
        return suffix[1:]
    else:
        logger.error(f"不支持的文件类型 {suffix or '<无后缀>'}，仅支持: pdf, pptx, docx, md")
        raise ValueError(f"不支持的文件类型 {suffix or '<无后缀>'}，仅支持: pdf, pptx, docx, md")




def node_entry(state: ImportGraphState) -> ImportGraphState:
    """
    导入图入口节点：
    1. 校验调用方传入的业务 ID 和本地文件路径。
    2. 判断 pdf/pptx/docx/md/transcript_md。
    3. 提取文件标题。
    4. 把原文件上传到 MinIO  upload-docs 。
    """
    node_name = sys._getframe().f_code.co_name
    task_id = _required_state_value(state, "task_id")
    add_running_task(task_id, node_name)
    logger.info(f">>> [{node_name}] 开始执行")

    try:
        meeting_id = _required_state_value(state, "meeting_id")
        document_id = _required_state_value(state, "document_id")

        input_doc_path = Path(_required_state_value(state, "input_doc_path")).expanduser().resolve()
        if not input_doc_path.is_file():
            raise FileNotFoundError(f"文件不存在或不是普通文件: {input_doc_path}")

        file_type = detect_file_type(input_doc_path,state)
        # 上传文件到 MinIO
        original_doc_path = upload_document(
            input_doc_path,
            meeting_id=meeting_id,
            document_id=document_id,
        )

        state["file_type"] = file_type
        state["file_title"] = input_doc_path.stem
        state["original_doc_path"] = original_doc_path
        # ImportGraphState 对路径字段的约定是字符串；节点内部需要路径操作时再转为 Path。
        state["output_local_dir"] = str(PROJECT_ROOT / "output" / meeting_id / document_id)

    except Exception:
        logger.exception(f">>> [{node_name}] 节点执行失败")
        raise
    finally:
        add_done_task(task_id, node_name)
        logger.info(f">>> [{node_name}] 节点执行完成")

    return state
