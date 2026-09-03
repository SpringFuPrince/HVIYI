import mimetypes
from pathlib import Path
from threading import Lock

from minio import Minio

from app.conf.minio_config import minio_config
from app.utils.logger import logger


_minio_client: Minio | None = None
_client_lock = Lock()


def get_minio_client() -> Minio:
    """惰性创建客户端；导入模块时不连接 MinIO。"""
    global _minio_client

    if _minio_client is None:
        with _client_lock:
            if _minio_client is None:
                minio_config.validate()
                _minio_client = Minio(
                    endpoint=minio_config.endpoint,
                    access_key=minio_config.access_key,
                    secret_key=minio_config.secret_key,
                    secure=minio_config.minio_secure,
                )
    return _minio_client


def ensure_bucket_exists(client: Minio | None = None) -> None:
    """首次上传时确保 Bucket 存在，不自动修改 Bucket 的访问策略。"""
    client = client or get_minio_client()
    bucket_name = minio_config.bucket_name
    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)
        logger.info(f"MinIO Bucket [{bucket_name}] 创建成功")


def _safe_segment(value: str | None, field_name: str) -> str:
    segment = str(value or "").strip()
    if not segment:
        raise ValueError(f"构建 MinIO 对象路径时缺少 {field_name}")
    if segment in {".", ".."} or "/" in segment or "\\" in segment:
        raise ValueError(f"{field_name} 不能包含路径分隔符: {segment}")
    return segment


def build_document_object_name(
    *,
    meeting_id: str,
    document_id: str,
    filename: str,
) -> str:
    """生成原文件对象名：upload-docs/租户/会议/文档/文件名。"""
    safe_filename = Path(filename).name
    if not safe_filename:
        raise ValueError("文件名不能为空")

    return "/".join(
        [
            minio_config.minio_doc_dir,
            _safe_segment(meeting_id, "meeting_id"),
            _safe_segment(document_id, "document_id"),
            safe_filename,
        ]
    )


def upload_local_file(local_file_path: str | Path, object_name: str) -> str:
    """把一个本地文件上传到默认 Bucket，成功后返回 object_name。"""
    file_path = Path(local_file_path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"待上传文件不存在或不是普通文件: {file_path}")

    client = get_minio_client()
    ensure_bucket_exists(client)

    content_type, _ = mimetypes.guess_type(file_path.name)
    kwargs = {"content_type": content_type} if content_type else {}
    client.fput_object(
        minio_config.bucket_name,
        object_name,
        str(file_path),
        **kwargs,
    )
    logger.info(
        f"文件已上传到 MinIO: bucket={minio_config.bucket_name}, "
        f"object_name={object_name}"
    )
    return object_name


def upload_document(
    local_file_path: str | Path,
    *,
    meeting_id: str,
    document_id: str,
) -> str:
    """构建 upload-docs 对象名并上传原文件。"""
    file_path = Path(local_file_path)
    object_name = build_document_object_name(
        meeting_id=meeting_id,
        document_id=document_id,
        filename=file_path.name,
    )
    return upload_local_file(file_path, object_name)
