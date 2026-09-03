import mimetypes
import os
import re
import sys
import base64
from pathlib import Path
from typing import List, Tuple
from collections import deque
from minio.deleteobjects import DeleteObject
from app.clients.minio_utils import get_minio_client
from app.graph.import_process.agent.state import ImportGraphState
from app.utils.task_utils import add_running_task, add_done_task
from app.lm.llm_utils import get_llm_client
from app.conf.minio_config import minio_config
from app.conf.lm_config import lm_config
from app.utils.logger import logger
# api访问限速工具
from app.utils.rate_limit_utils import apply_api_rate_limit
from app.utils.load_prompt import load_prompt

# MinIO支持的图片格式集合
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif"}

def is_supported_image(filename: str) -> bool:
    """
    判断文件是否为MinIO支持的图片格式（后缀不区分大小写）
    :param filename: 文件名（含后缀）
    :return: 支持返回True，否则False
    """
    return os.path.splitext(filename)[1].lower() in IMAGE_EXTENSIONS


def validate_path(state : ImportGraphState) -> Tuple[str,Path,Path]:

    md_file_path = state.get("md_path")

    if not md_file_path:
        state["md_path"] = state["input_doc_path"]
        md_file_path = state["md_path"]
    md_path_obj = Path(md_file_path)
    if not md_path_obj.exists():
        raise ValueError(f"Markdown 文件路径不存在: {md_file_path}")

    # 当上传的文件是Markdown时，state['md_content']为空，需要读取文件内容并赋值
    if not state.get('md_content'):
        with md_path_obj.open('r',encoding='utf-8') as f:
            state['md_content'] = f.read()

    images_dir_obj = md_path_obj.parent / "images"
    return state['md_content'],md_path_obj,images_dir_obj


def get_image_content(md_content, image_file,context_lenth=100):
    """
    获取Markdown中单张图片的上下文
    :param md_content: Markdown 内容字符串
    :param image_file: 图片文件名（含后缀）
    :param context_lenth: 上下文大小（默认100个字符）
    :return: 上下文元组（上文,下文）
    """
    pattern = re.compile(
        rf"!\[.*?\]\(.*?{image_file}.*?\)"
    )

    content = None
    items = list(pattern.finditer(md_content))
    if not items:
        return None
    if item := items[0]:
        start ,end =  item.span()
        pre_text = md_content[max(start-context_lenth,0):start]
        post_text = md_content[end:min(end+context_lenth,len(md_content))]
        content = (pre_text,post_text)
    if content:
        logger.info(f"[find_image_in_md_content] 图片{image_file}获取上下文成功")
    return content



def scan_image(md_content : str, images_dir_obj : Path) -> List[Tuple[str,str,Tuple[str,str]]]:
    """
    扫描Markdown中使用的照片，整理成(图片名, 图片地址, 上下文元组)的列表
    :param md_content: Markdown 内容字符串
    :param images_dir_obj: 图片目录Path对象
    :return: 图片列表，每个元素为(图片名, 图片地址, 上下文元组)
    """

    targets = []
    #循环images_dir_obj下的所有文件，判断是否 in md_content，如果是，截取上下文
    for image_file in os.listdir(images_dir_obj):
        # 检查图片格式
        if not is_supported_image(image_file):
            logger.warning(f"[scan_image]图片 {image_file} 不是支持的图片格式,跳过")
            continue

        #（上文，下文）
        context_data = get_image_content(md_content,image_file)
        if not context_data:
            logger.warning(f"[scan_image]图片 {image_file} 未在Markdown中引用,跳过")
            continue
        targets.append((image_file,str(images_dir_obj / image_file),context_data))
    return targets


def generate_image_summaries(targets,md_file_name):
    """
    生成图片描述
    :param targets: (图片名, 图片地址, 上下文元组)
    :param md_file_name: Markdown 文件名（无后缀）
    :return: {图片名.xx:图片描述,图片名.yy:图片描述,...}
    """
    summaries = {}
    # api访问限速
    request_time = deque()

    #循环每张图片，invoke模型，生成描述
    for image_file,image_path,context in targets:
        #1.访问限制，每分钟10次
        apply_api_rate_limit(request_time,max_requests=10)

        #获取模型对象
        vl_mode = get_llm_client(model = lm_config.vl_model)
        #获取提示词
        prompt_text = load_prompt(
            name="image_summary",
            root_folder=md_file_name,
            image_content=context
        )

        #读取图片内容，转换为base64编码
        with open(image_path,'rb') as f:
            image_base64 = base64.b64encode(f.read()).decode('utf-8')

        message = [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt_text
                },
                {
                    "type": "input_image",
                    #直接放图片的网络地址
                    #或图片的base64编码
                    "input_image": f"data:image/jpeg;base64,{image_base64}",
                },
            ],
        }]
        #调用模型
        response = vl_mode.invoke(message)
        summary = response.content.strip().replace("\n","")
        summaries[image_file] = summary
        logger.info(f"[generate_image_summaries]图片 {image_file} 生成描述: {summary}")
    logger.info(f"[generate_image_summaries]成功生成 {len(summaries)} 张图片的描述")
    return summaries


def upload_images_and_replace_md(meeting_id,document_id,summaries, targets, md_content, stem):
    """
    上传图片到MinIO，并替换Markdown中的图片链接为MinIO URL
    :param summaries: 图片描述字典
    :param targets: 图片目标列表，每个元素为(图片名, 原图片地址, 上下文元组)
    :param md_content: Markdown内容字符串
    :param stem: Markdown文件名（无后缀）
    :return: 新的Markdown内容字符串
    """

    prefix = f"{minio_config.minio_img_dir}/{meeting_id}/{document_id}/{stem}/"

    #2. 上传图片到MinIO
    minio_client = get_minio_client()

    #   先获取所有对象
    object_list = minio_client.list_objects(
        minio_config.bucket_name,
        prefix=prefix,
        recursive=True
    )
    #   构建删除对象列表
    delete_object_list = [DeleteObject(obj.object_name) for obj in object_list]
    #   删除所有对象
    errors = minio_client.remove_objects(minio_config.bucket_name, delete_object_list)
    for error in errors:
        logger.error(f"[upload_images_and_replace_md]删除图片 {error.object_name} 失败: {error.error}")
    logger.info(f"[upload_images_and_replace_md]成功删除 {prefix} 下的 {len(delete_object_list)} 张图片")

    # 上传图片
    #     声明上传图片字典
    #     image_url:图片名:图片URL
    #     summaries:图片名:图片描述
    image_url = {}
    for image_file,image_path,_ in targets:
        try:
            content_type, _ = mimetypes.guess_type(image_path)
            if content_type is None:
                content_type = "application/octet-stream"
            minio_client.fput_object(
                object_name=f"{prefix}{image_file}",
                bucket_name=minio_config.bucket_name,
                file_path=image_path,
                content_type=content_type,
            )
            image_url[image_file] = f"http://{minio_config.endpoint}/{minio_config.bucket_name}/{prefix}{image_file}"
            logger.info(f"[upload_images_and_replace_md]成功上传图片 {image_file} 到 {image_url[image_file]}")
        except Exception as e:
            logger.error(f"[upload_images_and_replace_md]上传图片 {image_file} 失败: {e}")
            continue

    #3. 替换Markdown中的图片链接为MinIO URL
    #     图片信息汇总
    image_infos = {}
    for image_file,summary in summaries.items():
        if url := image_url.get(image_file):
            image_infos[image_file] = (summary,url)
    logger.info(f"[upload_images_and_replace_md]结果汇总: {image_infos}")
    for image_file,(summary,url) in image_infos.items():

        rep = re.compile(
            rf"!\[.*?\]\(.*?{re.escape(image_file)}.*?\)"
        )

        md_content = rep.sub(
            lambda m: f"![{summary}]({url})",
            md_content
        )
        logger.info(f"[upload_images_and_replace_md]成功替换图片 {image_file} 为 {url}")
    return md_content


def replace_md_and_save(new_md_content, md_path_obj, state: ImportGraphState):
    """
    创建新的MD文件为 xxx_new.md
    :param new_md_content: 新的Markdown内容字符串
    :param md_path_obj: 原始MD文件路径对象
    :return: 新的MD文件路径字符串
    """
    output_local_dir = Path(state["output_local_dir"])
    output_local_dir.mkdir(parents=True, exist_ok=True)
    new_md_path = output_local_dir / f"{md_path_obj.stem}_new.md"
    with open(new_md_path, "w", encoding="utf-8") as f:
        f.write(new_md_content)
    logger.info(f"[replace_md_and_save]成功创建新的MD文件 {new_md_path}")
    return new_md_path




def node_md_img(state: ImportGraphState) -> ImportGraphState:
    """
    图片处理
    """
    node_name = sys._getframe().f_code.co_name
    logger.info(f">>> [{node_name}]开始执行")
    add_running_task(state["task_id"], node_name)
    try:
        md_content,md_path_obj,images_dir_obj = validate_path(state)

        if not images_dir_obj.exists():
            logger.info(f"[{node_name}]没有图片目录，文档没有图片，直接跳过该节点")
            return state

        # 识别md中使用的照片，整理成
        # [   (   图片名,   图片地址,  (上文,下文) ) , (   图片名,   图片地址,  (上文,下文) )  ]
        targets = scan_image(md_content,images_dir_obj)

        # 生成图片描述
        summaries = generate_image_summaries(targets,md_path_obj.stem)

        # 上传图片到MinIO，并替换图片链接为MinIO URL
        new_md_content = upload_images_and_replace_md(state["meeting_id"],state["document_id"],summaries,targets,md_content,md_path_obj.stem)

        # 创建新的MD文件为 xxx_new.md
        new_md_file_path = replace_md_and_save(new_md_content,md_path_obj, state)
        # 更新state
        state["md_path"] = new_md_file_path
        state["md_content"] = new_md_content
        return state
    except Exception as e:
        logger.error(f"[{node_name}] 处理失败，异常信息: {e}")
        raise
    finally:
        logger.info(f">>> [{node_name}]节点执行完成")
        add_done_task(state["task_id"], node_name)
