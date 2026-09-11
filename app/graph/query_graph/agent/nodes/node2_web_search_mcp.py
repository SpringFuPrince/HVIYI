import asyncio
import json
import sys
from agents.mcp import MCPServerStreamableHttp # openai-agents
from app.utils.logger import  logger

from app.conf.bailian_mcp_config import mcp_config
from app.utils.task_utils import add_running_task,add_done_task




async def mcp_call_streamable(query: str):
    search_mcp = MCPServerStreamableHttp(
        name = "search_mcp",
        params={
            "url": mcp_config.mcp_base_url,
            "headers":{"Authorization": f"Bearer {mcp_config.api_key}"},
            "timeout":10,
        },
        max_retry_attempts=3
    )
    try:
        await search_mcp.connect()
        tools = await search_mcp.list_tools()
        logger.info(f"工具列表: {tools}")
        result = await search_mcp.call_tool(
            tool_name="bailian_web_search",
            arguments={
                "query": query,
                "count": 5,
            }
        )
        return result
    finally:
        await search_mcp.cleanup()


async def node_web_search_mcp(state):
    """
    调用外部搜索引擎补充信息
    :param state:
    :return:
    """
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name, state.get("is_stream", True))

    try:
        query = state.get("rewritten_query")
        result = await mcp_call_streamable(query)
        text = (result.content[0].text or "") if (result and result.content) else ""
        web_documents = json.loads(text).get("pages", [])

    except (json.JSONDecodeError, AttributeError, IndexError) as e:
        logger.warning(f"网络搜索结果解析失败，返回空列表：{e}")
        web_documents = []
    finally:
        logger.info(f">>> [{node_name}]节点执行完成")

    add_done_task(state["task_id"], node_name, state.get("is_stream", False))
    return {"web_search_docs": web_documents}


if __name__ == '__main__':

    from dotenv import load_dotenv
    load_dotenv()
    test_state = {
        "task_id":"mcp_01",
        "rewritten_query": "AI Agent",
        "is_stream":True
    }

    # 调用 websearch_node 函数
    result_state = asyncio.run(node_web_search_mcp(test_state))

    # 验证结果
    print("测试结果:")
    print(f"查询内容: {test_state.get('rewritten_query')}")

    # 输出搜索结果
    search_results = result_state.get('web_search_docs', [])
    print(f"搜索结果数量: {len(search_results)}")
    print("search_results", search_results)
