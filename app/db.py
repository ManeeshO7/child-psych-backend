from prisma import Prisma

_db: Prisma | None = None


async def connect_db() -> None:
    global _db
    if _db is None:
        _db = Prisma()
        await _db.connect()


async def get_db() -> Prisma:
    global _db
    if _db is None:
        await connect_db()
    return _db


async def disconnect_db() -> None:
    global _db
    if _db is not None:
        await _db.disconnect()
        _db = None
