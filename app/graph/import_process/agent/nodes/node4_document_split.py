import re
import json
import os
import sys
# 统一类型注解，避免混用any/Any
# LangChain文本分割器（标注核心用途，便于理解）
from langchain_text_splitters import RecursiveCharacterTextSplitter

# 项目内部工具/状态/日志导入（保持原有路径）
from app.utils.task_utils import add_running_task, add_done_task
from app.graph.import_process.agent.state import ImportGraphState
from app.utils.logger import logger  # 项目统一日志工具，核心替换print

# --- 配置参数 (Configuration) ---
# 单个Chunk最大字符长度：超过则触发二次切分（适配大模型上下文窗口）
MAX_CONTENT_LENGTH = 2000
# 短Chunk合并阈值：同父标题的短Chunk会被合并，减少碎片化
MIN_CONTENT_LENGTH = 500


def validate_content(state):
    """
    校验要切片的内容。
    """
    md_content = state.get("md_content")

    if not md_content :
        logger.error("[validate_content] md_content为空")
        raise ValueError("[validate_content] md_content为空")
    md_content = md_content.replace("\r\n", "\n").replace("\r", "\n")
    file_title = state.get("file_title", "default_file")

    return md_content, file_title


def split_by_title(md_content, file_title):
    """
     语义切割
     基于标题切割文档

    {
        "title": #1 >> #1.2 >> #1.2.1,
        "content": 内容,
        "file_title": 文件名
    }
    因为后期还要细切，所以这里层级标题没有加到content中，细切完再把层级标题和content合并起来。

    """

    title_regex = r"^\s*(#{1,6})\s+(.+)"
    # 按行分割
    lines = md_content.splitlines()
    chunks = []
    # [(标题等级, 标题内容), (标题等级, 标题内容), ...]
    title_stack: list[tuple[int, str]] = []
    # 章节内容[段落1, 段落2, ...]
    current_content: list[str] = []

    title_count = 0
    is_code_block = False

    def save_chunk():
        """
        保存当前章节
        """
        content = "\n".join(current_content).strip()
        current_content.clear()
        if not content:
            return

        if title_stack:
            title = " >> ".join(item[1] for item in title_stack)
        else:
            # 文档顶部前可能存在的前言
            title = file_title

        chunks.append(
            {
                "title": title,
                "content": content,
                "file_title": file_title,
            }
        )

    for line in lines:
        stripped_line = line.strip()

        # 代码块中的#不识别为标题
        if stripped_line.startswith("```") or stripped_line.startswith("~~~"):
            is_code_block = not is_code_block # 取饭
            current_content.append(line)
            continue

        if is_code_block:
            match = None
        else:
            match = re.match(title_regex, stripped_line)
        if not match:
            current_content.append(line)
            continue

        # 先保存上一个标题对应的正文
        save_chunk()

        title_count += 1
        level = len(match.group(1))

        # 删除同级标题和它下面的子标题
        while title_stack and title_stack[-1][0] >= level:
            title_stack.pop()

        title_stack.append((level, stripped_line))

        # 保存文档最后一个章节
    save_chunk()

    return chunks, title_count, len(lines)


def split_long_chunk(chunks,max_content_length:int):
    """
    对超过最大长度的chunk进行二次切分。
    """
    final_chunks = []
    for chunk in chunks:
        content = chunk["content"]

        if len(content) <= max_content_length:
            final_chunks.append(chunk)
            continue
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=max_content_length,
            chunk_overlap=100,
            separators=['\n\n', '\n', '。', '！', '；', '']
        )
        for item in splitter.split_text(content):
            final_chunks.append({
                "content": item.strip(),
                "title": chunk["title"],
                "file_title": chunk["file_title"]
            })
    return final_chunks


def merge_short_chunks(chunks, min_content_length:int,max_content_length:int):
    """
    把小于MIN_CONTENT_LENGTH的chunk，合并起来
    必须是同一个parent_title的chunk
    """
    result = []
    
    for chunk in chunks:

        # 第一个chunk直接添加
        if not result:
            result.append(chunk)
            continue

        last = result[-1]

        can_merge = (
            len(last["content"]) < min_content_length
            and
            last["title"] == chunk["title"]
            and
            len(last["content"]) + len(chunk["content"]) <= max_content_length
        )
        if can_merge:
            last["content"] += "\n\n" + chunk["content"]

        else:
            result.append(chunk)
    return result

def adjust_chunk(chunks):

    result = []
    current_part = 1
    for chunk in chunks:
        if result and result[-1]["title"] == chunk["title"]:
            current_part += 1
        else:
            current_part = 1

        text = chunk["title"] + "\n" + chunk["content"]
        title = chunk["title"]
        parent_title = chunk["title"].split(" >> ")[-1]
        part = current_part
        file_title = chunk["file_title"]
        result.append({
            "content": text,
            "title": title,
            "parent_title": parent_title,
            "part": part,
            "file_title": file_title
        })
    return result


def refine_chunks(chunks,min_content_length:int,max_content_length:int):
    """
    内容的精细化切割
    1.超过了MAX_CONTENT_LENGTH的chunk，需要二次切分 （parent_title | part）
    2.小于MIN_CONTENT_LENGTH的chunk，合并
    :param chunks: 输入的文档列表，每个文档包含标题和内容。
    :param min_content_length: 最小chunk长度阈值。
    :param max_content_length: 最大chunk长度阈值。
    :return: chunks。
    """""
    final_chunks = []

    # 1.超过切分
    final_chunks = split_long_chunk(chunks,max_content_length)

    # 2.小于合并
    final_chunks = merge_short_chunks(final_chunks,min_content_length,max_content_length)

    final_chunks = adjust_chunk(final_chunks)
    logger.info(f"adjust_chunk 调整完成，最终切分了 {len(final_chunks)} 个块")
    return final_chunks



def node_document_split(state: ImportGraphState) -> ImportGraphState:
    """
    节点: 文档切分 (node_document_split)

    1. 基于 Markdown 标题层级进行递归切分。
    2. 对过长的段落进行二次切分。
    3. 生成面包屑导航的Chunk列表。
    """
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name)

    try:
        # 1.校验数据
        md_content ,file_title = validate_content(state)
        # 2.粗切，基于标题切割，保证语义
        #   [{content,title,file_title},{……}]
        chunks,title_count,lines_count =  split_by_title(md_content, file_title)
        # 3.处理没有标题文本的情况
        if title_count == 0:
            chunks = [{
                "title": "无标题",
                "content": md_content,
                "file_title": file_title
            }]
        # 细切
        chunks = refine_chunks(chunks,MIN_CONTENT_LENGTH,MAX_CONTENT_LENGTH)

        state["chunks"] = chunks

    except Exception as e:
        logger.error(f"[{node_name}] 执行过程中发生错误: {e}")
        raise
    finally:
        logger.info(f">>> [{node_name}]节点执行完成")
        add_done_task(state["task_id"], node_name)  # 推送信息到前端


    return state


