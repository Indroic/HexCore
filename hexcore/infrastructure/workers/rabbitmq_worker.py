"""
Worker asíncrono para RabbitMQ. 
Resuelve el problema de la duplicación de conexiones (SQLAlchemy y MongoDB)
garantizando que todos los pools se inicializan *dentro* del nuevo Event Loop.
"""
import asyncio
import logging
import typing as t
from contextlib import AsyncExitStack, asynccontextmanager

from hexcore.config import LazyConfig

logger = logging.getLogger("hexcore.workers.rabbitmq")


@asynccontextmanager
async def setup_sqlalchemy():
    """Context manager para inicializar y limpiar conexiones SQL en el worker."""
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
        dispose_engine,
        init_engine,
    )

    # 1. Descartar cualquier pool heredado y crear el engine dentro de ESTE event loop.
    logger.info("[Worker Lifecycle] Inicializando engine de SQLAlchemy...")
    await dispose_engine()
    init_engine()

    try:
        yield
    finally:
        logger.info("[Worker Lifecycle] Cerrando conexiones de SQLAlchemy...")
        await dispose_engine()


@asynccontextmanager
async def setup_mongodb():
    """Context manager para inicializar Beanie/MongoDB en el worker."""
    from hexcore.infrastructure.repositories.orms.beanie.utils import init_beanie_documents
    
    logger.info("[Worker Lifecycle] Inicializando MongoDB (Beanie)...")
    await init_beanie_documents()
    
    try:
        yield
    finally:
        # Beanie/Motor no requiere un teardown estricto manual ya que se cierra
        # con el event loop o GC, pero aquí podríamos cerrar el AsyncMongoClient si tuvieramos la ref.
        logger.info("[Worker Lifecycle] MongoDB (Beanie) ciclo finalizado.")


@asynccontextmanager
async def connect_rabbitmq(amqp_url: str):
    """Context manager para manejar la conexión de aio-pika."""
    import aio_pika
    
    logger.info("[Worker Lifecycle] Conectando a RabbitMQ...")
    connection = await aio_pika.connect_robust(amqp_url)
    try:
        yield connection
    finally:
        logger.info("[Worker Lifecycle] Cerrando conexión de RabbitMQ...")
        await connection.close()


async def run_worker(
    amqp_url: str,
    event_bus_factory: t.Callable[["aio_pika.abc.AbstractRobustConnection"], t.Any],
) -> None:
    """
    Punto de entrada robusto para arrancar el Worker de RabbitMQ.
    Asegura que no haya "magia negra" (fugas de conexiones o error de threads).
    
    Args:
        amqp_url: URL de RabbitMQ (ej: amqp://guest:guest@localhost/)
        event_bus_factory: Función/Callback que recibe la conexión aio-pika e 
                           instancia un RabbitMQEventBus con sus handlers registrados.
    """
    logger.info("Iniciando Worker de EventBus...")
    
    async with AsyncExitStack() as stack:
        # 1. Setup DataBases (SQL y NoSQL) atados a ESTE Loop
        await stack.enter_async_context(setup_sqlalchemy())
        await stack.enter_async_context(setup_mongodb())
        
        # 2. Setup RabbitMQ
        rmq_conn = await stack.enter_async_context(connect_rabbitmq(amqp_url))
        
        # 3. Construir e inicializar el bus
        event_bus = event_bus_factory(rmq_conn)
        
        # 4. Comenzar a consumir
        await event_bus.start_consuming()
        
        # 5. Mantener vivo (forever)
        logger.info("Worker corriendo eternamente. Presiona Ctrl+C para salir.")
        await asyncio.Future()  # bloquea infinitamente hasta que se envíe una señal
