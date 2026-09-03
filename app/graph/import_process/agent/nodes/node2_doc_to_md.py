import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

import requests

from app.utils.logger import logger, PROJECT_ROOT
from app.graph.import_process.agent.state import ImportGraphState, create_default_state
from app.utils.task_utils import add_running_task, add_done_task
from app.conf.mineru_config import mineru_config


MINERU_BASE_URL = mineru_config.base_url
MINERU_API_TOKEN = mineru_config.api_key

############
#path       是指文件的字符串路径
#path_obj   是指文件的路径的Path对象
#dir_obj    是指文件所在目录的Path对象
#dir        是指文件所在目录的字符串路径
#############


def validate_path(state: ImportGraphState):
    """
    校验文档路径和输出目录
    """
    # Graph State 约定存字符串，但这里兼容 PathLike，避免节点间类型差异导致 .strip() 异常。
    input_doc_path = state.get("input_doc_path")
    output_local_dir = state.get("output_local_dir")

    if input_doc_path is None or not str(input_doc_path).strip():
        logger.error(f"input_doc_path为空")
        raise ValueError(f"input_doc_path为空")
    if output_local_dir is None or not str(output_local_dir).strip():
        logger.error("output_local_dir为空")
        raise ValueError("output_local_dir为空")

    # 转换为Path对象
    input_doc_path_obj = Path(input_doc_path).expanduser()
    output_local_dir_obj = Path(output_local_dir).expanduser()

    # 原始文档文件路径存在校验
    if not input_doc_path_obj.exists():
        logger.error(f"文档文件不存在，绝对路径：{input_doc_path_obj.absolute()}")
        raise FileNotFoundError(f"文档文件不存在，绝对路径：{input_doc_path_obj.absolute()}")
    if not input_doc_path_obj.is_file():
        raise FileNotFoundError(f"指定路径非文件（是目录），绝对路径：{input_doc_path_obj.absolute()}")

    # 输出目录存在校验
    if not output_local_dir_obj.exists():
        logger.info(f"输出目录不存在，自动创建{output_local_dir_obj}")
        output_local_dir_obj.mkdir(parents=True, exist_ok=True)

    return  input_doc_path_obj , output_local_dir_obj


def upload_and_pool(input_doc_path_obj) -> str:
    """
    上传文档文件到MinerU
    :param input_doc_path_obj: 文档文件Path对象
    :return: 解析后的zip包URL
    """
    # 前置配置校验，拦截无效配置
    if not MINERU_BASE_URL or not MINERU_API_TOKEN:
        raise ValueError("MinerU配置缺失：请在.env中正确配置MINERU_BASE_URL和MINERU_API_TOKEN")
    logger.info(f"[配置校验] MinerU基础配置加载成功，开始处理文件：{input_doc_path_obj.name}")

    # 构造请求头（符合HTTP规范，Bearer鉴权）
    request_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MINERU_API_TOKEN}"
    }

    # 1. 调用批量接口，获取上传Signed URL和任务batch_id
    url_get_upload = f"{MINERU_BASE_URL}/file-urls/batch"
    req_data = {
        "files": [{"name": input_doc_path_obj.name}],
        "model_version": "vlm"  # 官方推荐解析模型
    }
    logger.debug(f"[获取上传链接] 调用接口：{url_get_upload}，请求参数：{req_data}")
    resp = requests.post(url=url_get_upload, headers=request_headers, json=req_data, timeout=30)

    # 响应校验：先验HTTP状态，再验业务返回码
    if resp.status_code != 200:
        raise RuntimeError(f"[获取上传链接] 网络请求失败，状态码：{resp.status_code}，响应内容：{resp.text}")

    resp_data = resp.json()
    if resp_data["code"] != 0:
        raise RuntimeError(f"[获取上传链接] API业务错误，返回数据：{resp_data}")

    # 提取核心数据：上传链接和任务唯一标识
    signed_url = resp_data["data"]["file_urls"][0]
    batch_id = resp_data["data"]["batch_id"]
    logger.info(f"[获取上传链接] 成功，batch_id：{batch_id}，上传链接已生成")

    # 2. 读取原始文档二进制数据，准备上传
    logger.info(f"[文件上传] 开始读取原始文档文件：{input_doc_path_obj.name}")
    with open(input_doc_path_obj, "rb") as f:
        file_data = f.read()

    # 创建Session（复用TCP连接，禁用代理避免签名验证失败）
    upload_session = requests.Session()
    upload_session.trust_env = False

    try:
        # 首次上传：自动识别文件类型
        put_resp = upload_session.put(url=signed_url, data=file_data, timeout=60)
        # 重试逻辑：首次失败则强制指定PDF的Content-Type
        if put_resp.status_code != 200:
            logger.warning(f"[文件上传] 首次上传失败（状态码：{put_resp.status_code}），强制指定PDF类型重试")
            pdf_headers = {"Content-Type": "application/pdf"}
            put_resp = upload_session.put(url=signed_url, data=file_data, headers=pdf_headers, timeout=60)
            # 重试仍失败则抛出异常
            if put_resp.status_code != 200:
                raise RuntimeError(f"[文件上传] 重试后仍失败，状态码：{put_resp.status_code}，响应内容：{put_resp.text}")
        logger.info(f"[文件上传] 成功，文件{input_doc_path_obj.name}已存入云存储")
    except Exception as e:
        raise RuntimeError(f"[文件上传] 网络异常导致上传失败，错误信息：{str(e)}")
    finally:
        # 无论成败，关闭Session释放网络连接，避免资源泄漏
        upload_session.close()

    # 3. 根据batch_id轮询任务状态，直至完成/失败/超时
    poll_url = f"{MINERU_BASE_URL}/extract-results/batch/{batch_id}"
    start_time = time.time()
    timeout_seconds = 600  # 最大超时时间10分钟（适配600页内PDF）
    poll_interval = 3  # 轮询间隔3秒（平衡查询频率和服务端压力）
    logger.info(f"[任务轮询] 开始监控任务状态，batch_id：{batch_id}，最大超时：{timeout_seconds}s")

    while True:
        # 超时检查：超过最大时间直接终止轮询
        elapsed_time = time.time() - start_time
        if elapsed_time > timeout_seconds:
            raise TimeoutError(f"[任务轮询] 超时！任务处理超{int(timeout_seconds)}秒，batch_id：{batch_id}")

        # 发起轮询请求，短超时10秒，异常则重试
        try:
            poll_resp = requests.get(url=poll_url, headers=request_headers, timeout=10)
        except Exception as e:
            logger.warning(f"[任务轮询] 网络请求异常，{poll_interval}秒后重试：{str(e)}")
            time.sleep(poll_interval)
            continue

        # 处理HTTP响应错误：5xx服务端繁忙则重试，其他错误直接抛出
        if poll_resp.status_code != 200:
            if 500 <= poll_resp.status_code < 600:
                logger.warning(f"[任务轮询] 服务端繁忙（状态码：{poll_resp.status_code}），{poll_interval}秒后重试")
                time.sleep(poll_interval)
                continue
            else:
                raise RuntimeError(f"[任务轮询] HTTP请求失败，状态码：{poll_resp.status_code}，响应内容：{poll_resp.text}")

        # 解析轮询结果，校验业务状态
        poll_data = poll_resp.json()
        if poll_data["code"] != 0:
            raise RuntimeError(f"[任务轮询] API业务错误，返回数据：{poll_data}")

        extract_results = poll_data["data"]["extract_result"]
        # 结果暂空，继续轮询
        if not extract_results:
            logger.debug(f"[任务轮询] 结果暂为空，已耗时{int(elapsed_time)}s，继续等待")
            time.sleep(poll_interval)
            continue
        # 解析任务状态，分支处理
        result_item = extract_results[0]
        state_status = result_item["state"]
        # 状态1：任务完成，提取ZIP下载链接
        if state_status == "done":
            logger.info(f"[任务轮询] 解析任务完成！总耗时：{int(elapsed_time)}s，batch_id：{batch_id}")
            full_zip_url = result_item.get("full_zip_url")
            if not full_zip_url:
                raise RuntimeError("[任务轮询] 任务完成但未返回ZIP包下载链接，batch_id：{batch_id}")
            logger.info(f"[任务轮询] 结果ZIP包下载链接：{full_zip_url}...")
            return full_zip_url
        # 状态2：任务失败，提取错误信息抛出
        elif state_status == "failed":
            err_msg = result_item.get("err_msg", "未知错误，无具体信息")
            raise RuntimeError(f"[任务轮询] 解析任务失败，batch_id：{batch_id}，错误信息：{err_msg}")
        # 状态3：处理中，实时打印进度（覆盖当前行）
        else:
            logger.debug(
                f"[任务轮询] 处理中（已耗时{int(elapsed_time)}s）| 刷新间隔{poll_interval}s",
                end="\r"
            )
            time.sleep(poll_interval)

def download_and_extract(zip_url: str, output_local_dir_obj: Path, input_doc_path_stem: str) -> str:
    """
    下载并解压zip包

    :param zip_url: zip包URL
    :param output_local_dir_obj: 输出目录Path对象
    :param input_doc_path_stem: 文件标题（文件名去后缀）
    :return: Markdown文件地址
    """
    logger.info(f"开始处理[{input_doc_path_stem}]的MinerU解析结果")

    # 1. 下载解析结果ZIP包，120秒超时适配大文件
    logger.info(f"[步骤1/4] 开始下载ZIP包，链接：{zip_url}...")
    resp = requests.get(zip_url, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"[步骤1/4] ZIP包下载失败，HTTP状态码：{resp.status_code}")

    # 拼接ZIP包保存路径，按PDF名称唯一命名
    zip_save_path = output_local_dir_obj / f"{input_doc_path_stem}_result.zip"
    with open(zip_save_path, "wb") as f:
        f.write(resp.content)
    logger.info(f"[步骤1/4] ZIP包下载成功，保存路径：{zip_save_path}")

    # 2. 清理旧解压目录并解压ZIP包（避免旧文件干扰，为每个PDF创建专属目录）
    logger.info(f"[步骤2/4] 开始解压ZIP包...")
    extract_target_dir = output_local_dir_obj / input_doc_path_stem

    # 清理旧目录，异常则警告不终止
    if extract_target_dir.exists():
        try:
            # 递归删除整个目录树，包括目录本身及其所有子目录和文件。
            shutil.rmtree(extract_target_dir)
            logger.info(f"[步骤2/4] 已清理旧的解压目录：{extract_target_dir}")
        except Exception as e:
            logger.warning(f"[步骤2/4] 清理旧目录失败，可能不影响新文件解压：{str(e)}")

    # 重新创建解压目录
    extract_target_dir.mkdir(parents=True, exist_ok=True)

    # 核心解压操作，保留原目录结构
    with zipfile.ZipFile(zip_save_path, 'r') as zip_file_obj:
        # zip_save_path解压文件到extract_target_dir
        zip_file_obj.extractall(extract_target_dir)
    logger.info(f"[步骤2/4] ZIP包解压完成，解压目录：{extract_target_dir}")

    # 3. 递归查找解压目录下所有MD文件（适配子目录结构）
    logger.info(f"[步骤3/4] 开始查找解压目录中的MD文件...")
    md_file_list = list(extract_target_dir.rglob("*.md"))
    if not md_file_list:
        raise FileNotFoundError(f"[步骤3/4] 解压目录中未找到任何.md格式文件：{extract_target_dir}")
    logger.info(f"[步骤3/4] 共找到{len(md_file_list)}个MD文件，按优先级匹配目标文件")

    # 4. 按优先级匹配目标MD文件（同名→full.md→第一个，兜底避免流程中断）
    target_md_file = None
    # 优先级1：与PDF纯名称完全同名的MD文件
    for md_file in md_file_list:
        if md_file.stem == input_doc_path_stem:
            target_md_file = md_file
            logger.info(f"[步骤4/4] 匹配到优先级1目标：与PDF同名的MD文件 {target_md_file.name}")
            break
    # 优先级2：MinerU默认生成的full.md（不区分大小写）
    if not target_md_file:
        for md_file in md_file_list:
            if md_file.name.lower() == "full.md":
                target_md_file = md_file
                logger.info(f"[步骤4/4] 匹配到优先级2目标：MinerU默认文件 {target_md_file.name}")
                break
    # 优先级3：兜底取第一个MD文件
    if not target_md_file:
        target_md_file = md_file_list[0]
        logger.info(f"[步骤4/4] 未匹配到前两级目标，兜底取第一个MD文件 {target_md_file.name}")

    # 重命名MD文件：统一为PDF纯名称，便于后续流程处理（仅不同名时执行）
    if target_md_file.stem != input_doc_path_stem:
        logger.info(f"[步骤4/4] 开始重命名MD文件，统一为PDF同名：{input_doc_path_stem}.md")
        new_md_path = target_md_file.with_name(f"{input_doc_path_stem}.md")
        try:
            # 将磁盘上的文件进行重命名
            target_md_file.rename(new_md_path)
            # 更新变量引用
            target_md_file = new_md_path
            logger.info(f"[步骤4/4] MD文件重命名成功：{input_doc_path_stem}.md")
        except OSError as e:
            logger.warning(f"[步骤4/4] MD文件重命名失败，将使用原文件名继续流程：{str(e)}")

    # 转换为字符串绝对路径返回，适配后续仅支持字符串路径的函数
    final_md_path = str(target_md_file.absolute())
    logger.info(f"===== [{input_doc_path_stem}]解析结果处理完成，最终MD文件路径：{final_md_path} =====")
    return final_md_path


def node_doc_to_md(state: ImportGraphState) -> ImportGraphState:
    """
    文档转Markdown
    1. 校验 local_file_path | output_local_dir
    2. 调用 MinerU 解析文档。
    3. 下载zip包，解压并提取。
    4. 赋值 md_content | md_path。
    """
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name)  # 推送信息到前端

    try:
        #校验路径
        input_doc_path_obj,output_local_dir_obj = validate_path(state)
        #上传原始文档到MinerU
        zip_url = upload_and_pool(input_doc_path_obj)

        md_path = download_and_extract(zip_url,output_local_dir_obj,input_doc_path_obj.stem)

        state["md_path"] = str(md_path)
        state["output_local_dir"] = str(output_local_dir_obj)

        with open(md_path,"r",encoding="utf-8") as f:
            state["md_content"] = f.read()

    except Exception as e:
        logger.error(f"[{node_name}] 文档转换为Markdown失败，异常信息: {e}")
        raise
    finally:
        logger.info(f">>> [{node_name}]节点执行完成")
        add_done_task(state["task_id"], node_name)  # 推送信息到前端

    return state
