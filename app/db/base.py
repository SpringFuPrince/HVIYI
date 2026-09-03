from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """全部 MySQL ORM 模型共享的唯一 DeclarativeBase。"""

