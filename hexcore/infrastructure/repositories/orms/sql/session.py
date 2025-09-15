from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from hexcore.config import LazyConfig


# 1. CREAR EL ENGINE ASÍNCRONO DE SQLAlchemy
# Usamos create_async_engine en lugar de create_engine.
engine = create_async_engine(
    LazyConfig.get_config().async_sql_database_url,
    # `echo=True` es útil para depuración, ya que imprime todas las sentencias SQL.
    # Desactívalo en producción.
    # echo=True,
)

# 2. CREAR UNA FACTORÍA DE SESIONES ASÍNCRONAS
# Usamos async_sessionmaker y especificamos la clase AsyncSession.
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    class_=AsyncSession,
)


async def get_async_db_session():
    """Generador de dependencias para obtener una sesión de BD asíncrona."""
    db = AsyncSessionLocal()
    try:
        yield db
    finally:
        await db.close()
