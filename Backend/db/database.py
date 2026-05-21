from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import settings

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
# echo=True logs every SQL statement — useful during dev, turn off in prod
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=10,
    max_overflow=20,
)

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
# Use this everywhere you need a DB session:
#
#   async with async_session() as session:
#       session.add(some_object)
#       await session.commit()
#
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # keeps ORM objects usable after commit
)


# ---------------------------------------------------------------------------
# Base class for all models
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# FastAPI dependency — yields a session and auto-closes it
# ---------------------------------------------------------------------------
async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session