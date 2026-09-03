"""连接最近一次录音会话，验证当前 8002 进程能否加载流式模型。"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select
from websockets.asyncio.client import connect

from app.clients.mysql_utils import close_mysql_client, get_mysql_client
from app.db.memory_models import RecordingSession


async def main() -> None:
    try:
        async with get_mysql_client().session() as db:
            session = await db.scalar(
                select(RecordingSession)
                .where(RecordingSession.status == "recording")
                .order_by(RecordingSession.created_at.desc())
                .limit(1)
            )
        if session is None:
            raise RuntimeError("没有可用于诊断的recording会话")
        uri = f"ws://127.0.0.1:8002/ws/recordings/{session.session_id}"
        async with connect(uri, open_timeout=10) as websocket:
            message = await asyncio.wait_for(websocket.recv(), timeout=120)
        payload = json.loads(message)
        print("RUNNING_ASR_EVENT", json.dumps(payload, ensure_ascii=False))
        if payload.get("type") != "ready":
            raise RuntimeError(payload.get("message") or "流式ASR未就绪")
    finally:
        await close_mysql_client()


if __name__ == "__main__":
    asyncio.run(main())
