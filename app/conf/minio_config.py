import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _object_prefix(name: str, default: str) -> str:
    """MinIO 的“文件夹”实际是对象名前缀，不能以 / 开头或结尾。"""
    return (os.getenv(name, default) or default).strip().strip("/")


@dataclass(frozen=True)
class MinIOConfig:
    endpoint: str
    access_key: str
    secret_key: str
    bucket_name: str
    minio_img_dir: str
    minio_doc_dir: str
    minio_secure: bool

    def validate(self) -> None:
        required = {
            "MINIO_ENDPOINT": self.endpoint,
            "MINIO_ACCESS_KEY": self.access_key,
            "MINIO_SECRET_KEY": self.secret_key,
            "MINIO_BUCKET_NAME": self.bucket_name,
            "MINIO_IMG_DIR": self.minio_img_dir,
            "MINIO_DOC_DIR": self.minio_doc_dir,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"缺少 MinIO 环境变量: {', '.join(missing)}")


minio_config = MinIOConfig(
    endpoint=(os.getenv("MINIO_ENDPOINT") or "").strip(),
    access_key=(os.getenv("MINIO_ACCESS_KEY") or "").strip(),
    secret_key=(os.getenv("MINIO_SECRET_KEY") or "").strip(),
    bucket_name=(os.getenv("MINIO_BUCKET_NAME") or "").strip(),
    minio_img_dir=_object_prefix("MINIO_IMG_DIR", "upload-images"),
    minio_doc_dir=_object_prefix("MINIO_DOC_DIR", "upload-docs"),
    minio_secure=_env_bool("MINIO_SECURE"),
)
