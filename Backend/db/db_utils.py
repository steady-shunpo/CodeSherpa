import asyncio
from db.database import AsyncSessionLocal
from sqlalchemy import text

async def _do_status_update(run_id: str, status: str):
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE runs SET status = :status, updated_at = NOW() WHERE id = :run_id"),
            {"status": status, "run_id": run_id}
        )
        await session.commit()

def _set_status_sync(run_id: str, status: str, loop: asyncio.AbstractEventLoop):
    future = asyncio.run_coroutine_threadsafe(_do_status_update(run_id, status), loop)
    future.result(timeout=30)  # blocks the thread until DB write completes