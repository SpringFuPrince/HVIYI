"""使用 SQLAlchemy ORM 模型创建当前项目所需的全部 MySQL 表。

运行方式（在项目根目录）：
    uv run python prepare/creat_sql.py

脚本读取 .env 中的 MYSQL_URL（或 DATABASE_URL）。目标数据库必须已经存在；
``create_all`` 只创建缺失表，不会删除表，也不会修改已有表结构。
"""

import asyncio
import sys
from pathlib import Path

from sqlalchemy import inspect


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.clients.mysql_utils import close_mysql_client, get_mysql_client
from app.db.base import Base
import app.db.memory_models  # noqa: F401  注册全部 ORM 表到 Base.metadata


async def create_mysql_tables() -> None:
    """创建缺失表，并校验 ORM 中声明的表均已存在。"""

    client = get_mysql_client()
    try:
        await client.create_all(Base.metadata)

        engine = client.open()
        async with engine.connect() as connection:
            existing_tables = set(
                await connection.run_sync(
                    lambda sync_connection: inspect(sync_connection).get_table_names()
                )
            )

        expected_tables = set(Base.metadata.tables)
        missing_tables = sorted(expected_tables - existing_tables)
        if missing_tables:
            raise RuntimeError(
                "以下 ORM 表创建失败：" + ", ".join(missing_tables)
            )

        print(f"MySQL 建表完成，共确认 {len(expected_tables)} 张表：")
        for table_name in sorted(expected_tables):
            print(f"- {table_name}")
    finally:
        await close_mysql_client()


if __name__ == "__main__":
    asyncio.run(create_mysql_tables())
