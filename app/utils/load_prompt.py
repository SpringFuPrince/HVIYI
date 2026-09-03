from app.utils.path_util import PROJECT_ROOT
from app.utils.logger import logger  # 可选，加日志更友好

def load_prompt(name: str, **kwargs) -> str:
    """
    加载提示词并format变量
    :param name: 提示词文件名
    :param **kwargs: 需format的变量键值对（如root_folder="测试文件", image_content=("上文内容", "下文内容")）
    :return: format后的最终提示词字符串
    """
    # 拼接提示词路径
    prompt_path = PROJECT_ROOT / 'prompts' / f'{name}.prompt'

    # 校验文件是否存在
    if not prompt_path.exists():
        raise FileNotFoundError(f"提示词文件不存在：{prompt_path.absolute()}")

    # 读取纯文本提示词
    raw_prompt = prompt_path.read_text(encoding='utf-8')
    
    if kwargs:
        rendered_prompt = raw_prompt.format(**kwargs)
        logger.debug(f"提示词渲染成功，替换变量：{list(kwargs.keys())}")
        return rendered_prompt
    return raw_prompt



if __name__ == '__main__':
    root_folder = "hl3070使用说明书"
    image_content = ("《《   上文内容   》》", "《《    下文内容   》》")

    final_prompt = load_prompt(
        name='image_summary',
        root_folder=root_folder,
        image_content=image_content
    )
    print("最终提示词：")
    print(final_prompt)