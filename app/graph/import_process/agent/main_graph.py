from dotenv import load_dotenv
# 导入LangGraph核心依赖：StateGraph(状态图)、START/END(内置起始/结束节点常量)
from langgraph.graph import StateGraph, END

from app.utils.logger import logger
# 导入自定义状态类：统一管理工作流全程的所有数据（各节点共享/修改）
from app.graph.import_process.agent.state import ImportGraphState
# 导入所有自定义业务节点：每个节点对应知识库导入的一个具体步骤
from app.graph.import_process.agent.nodes.node1_entry import node_entry
from app.graph.import_process.agent.nodes.node2_doc_to_md import node_doc_to_md
from app.graph.import_process.agent.nodes.node3_md_img import node_md_img
from app.graph.import_process.agent.nodes.node4_document_split import node_document_split
from app.graph.import_process.agent.nodes.node7_meeting_meta import node_meeting_meta
from app.graph.import_process.agent.nodes.node5_bge_embedding import node_bge_embedding
from app.graph.import_process.agent.nodes.node6_import_milvus import node_import_milvus

load_dotenv()

workflow = StateGraph(ImportGraphState)

workflow.add_node("node_entry",node_entry)
workflow.add_node("node_doc_to_md",node_doc_to_md)
workflow.add_node("node_md_img",node_md_img)
workflow.add_node("node_document_split",node_document_split)
workflow.add_node("node_meeting_meta",node_meeting_meta)
workflow.add_node("node_bge_embedding",node_bge_embedding)
workflow.add_node("node_import_milvus",node_import_milvus)


workflow.set_entry_point("node_entry")
def route_after_entry(state: ImportGraphState) -> str:
    """
    根据文件类型路由
    md/transcript_md：node_md_img
    pdf/pptx/docx：node_doc_to_md
    其他文件：END
    """
    file_type = state.get("file_type")
    if file_type in {"pdf", "pptx", "docx"}:
        return "document"
    if file_type in {"md", "transcript_md"}:
        return "markdown"
    return END


def route_meeting_summary(state: ImportGraphState) -> str:
    file_type = state.get("file_type")
    if file_type == "transcript_md":
        return "node_meeting_meta"
    return END



workflow.add_conditional_edges(
    "node_entry",
    route_after_entry,
    {
        "document":"node_doc_to_md" ,
        "markdown":"node_md_img",
        END: END
    }
)


#5.定义静态边
workflow.add_edge("node_doc_to_md","node_md_img")
workflow.add_edge("node_md_img","node_document_split")
workflow.add_edge("node_document_split","node_bge_embedding")
workflow.add_edge("node_bge_embedding","node_import_milvus")

workflow.add_conditional_edges(
    "node_import_milvus",
    route_meeting_summary,
    {
        "node_meeting_meta":"node_meeting_meta",
        END: END
    }
)
workflow.add_edge("node_meeting_meta",END)

compiled_import_graph = workflow.compile()





async def main():
    from app.utils.path_util import PROJECT_ROOT
    import os

    logger.info("===== 开始执行导入全流程测试 =====")
    test_pdf_name = os.path.join("asset", "doc", "万用表RS-12的使用.pdf")
    test_pdf_path = os.path.join(PROJECT_ROOT, test_pdf_name)


    # 3. 校验测试PDF文件是否存在
    if not os.path.exists(test_pdf_path):
        logger.error(f"全流程测试失败：测试PDF文件不存在，路径：{test_pdf_path}")
    else:
        test_state = ImportGraphState(
            task_id="test_task_001",
            meeting_id="test_meeting_001",
            document_id="test_document_001",
            file_type="pdf",
            file_title="万用表RS-12的使用说明",
            input_doc_path=test_pdf_path,
            is_transcript=False,
        )
        try:

            final_state = None
            async for step in compiled_import_graph.astream(test_state, stream_mode="values"):

                current_node = list(step.keys())[-1] if step else "未知节点"
                logger.info(f"节点执行完成：{current_node}")
                final_state = step

            # 6. 全流程执行完成，结果预览和核心指标打印
            if final_state:
                logger.info("-" * 80)
                logger.info("===== 全流程测试执行成功，核心结果预览 =====")
                # 提取核心结果指标
                chunks = final_state.get("chunks", [])
                chunk_count = len(chunks)
                md_content = final_state.get("md_content", "")[:150]  # MD内容前150字符
                has_embedding = all("dense_vector" in c and "sparse_vector" in c for c in chunks) if chunks else False
                has_chunk_id = all("chunk_id" in c for c in chunks) if chunks else False
                kg_id = final_state.get("kg_id", "未生成")  # KG导入生成的ID（按实际业务字段调整）

                # 打印核心指标
                logger.info(f"📄 PDF转MD内容预览（前150字符）：{md_content}...")
                logger.info(f"📝 文档切分总切片数：{chunk_count}")
                logger.info(f"🔍 所有切片是否完成向量化：{'是' if has_embedding else '否'}")
                logger.info(f"🗄️  所有切片是否完成Milvus入库（含chunk_id）：{'是' if has_chunk_id else '否'}")
                logger.info(f"🧠 知识图谱导入ID：{kg_id}")
                logger.info(f"📂 最终状态包含的核心键：{list(final_state.keys())}")
                logger.info("-" * 80)
        except Exception:
            # 7. 异常捕获，打印详细错误信息
            logger.exception(f"===== 全流程测试运行失败 =====")
            raise
    logger.info("===== 知识图谱导入全流程测试结束 =====")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
