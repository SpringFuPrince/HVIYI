from pathlib import Path
from FlagEmbedding import FlagReranker
from app.conf.reranker_config import reranker_config

_reranker_model = None

def get_reranker_model():
    global _reranker_model
    if _reranker_model is None:
        model_path = Path(reranker_config.bge_reranker_large).resolve()
        if not model_path.exists():
            raise FileNotFoundError(f"Reranker 模型路径不存在: {model_path}")
        _reranker_model = FlagReranker(
            model_name_or_path=str(model_path).replace("\\", "/"),
            device=reranker_config.bge_reranker_device,
            use_fp16=reranker_config.bge_reranker_fp16,
        )
    return _reranker_model
