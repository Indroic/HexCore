"""
Kit de testing de Darwin: dobles de los puertos, contextos armados y un cableado sin base.

**El problema que resuelve.** Los tests de Darwin del propio framework corren contra SQLite, y ahí
es lo correcto: parte de lo que prueban es la atomicidad de las sentencias. Un consumidor no está
probando eso — está probando *su* caso de uso, que además pasa por auth. Para eso, levantar un
motor, crear seis tablas y borrarlas es tiempo y ceremonia por nada, y sobre todo es código que hay
que escribir bien una vez y copiar mal muchas.

Tres formas de usarlo, de menos a más:

1. **Un contexto suelto**, para probar lo que está aguas abajo de la autenticación::

       from hexcore.darwin import auth_scope
       from hexcore.darwin.testing import authenticated_context, make_user

       with auth_scope(authenticated_context(make_user(), scopes=["facturas:leer"])):
           resultado = await mi_caso_de_uso()

2. **Darwin cableado sin base**, para probar flujos::

       from hexcore.darwin import reset_identity
       from hexcore.darwin.testing import configure_test_identity, create_test_user

       contenedor = configure_test_identity()
       try:
           await create_test_user(contenedor, "ana@ejemplo.com")
           _, _, par = await contenedor.identity_service().sign_in(
               email="ana@ejemplo.com", password="una frase larga y buena"
           )
       finally:
           reset_identity()

3. **Las fixtures**, que hacen lo mismo y limpian solas. En tu `conftest.py`::

       pytest_plugins = ["hexcore.testing.fixtures", "hexcore.darwin.testing.fixtures"]

Se importa **sin dependencias opcionales duras**: `import hexcore.darwin.testing` funciona sin
sqlalchemy y sin fastapi. Lo que necesita `joserfc` —el cableado completo, porque emite tokens de
verdad— lo importa dentro de la función.

⚠️ **Nada de este módulo va a producción.** `PlainTextHasher` en particular no hashea: existe porque
Argon2id tarda ~100 ms a propósito y una suite con cincuenta sign-ins paga cinco segundos en KDF.
"""
from __future__ import annotations

from hexcore.darwin.testing.fakes import (
    AuditRecord,
    FakeAccountRepository,
    FakeRevocationList,
    FakeSessionRepository,
    FakeUserRepository,
    FakeVerificationRepository,
    PlainTextHasher,
    RecordingAuditSink,
)
from hexcore.darwin.testing.helpers import (
    TEST_SECRET_KEY,
    authenticated_context,
    configure_test_identity,
    create_test_user,
    impersonated_context,
    make_user,
    system_context,
)

__all__ = [
    # Dobles de los puertos
    "FakeUserRepository",
    "FakeSessionRepository",
    "FakeAccountRepository",
    "FakeVerificationRepository",
    "FakeRevocationList",
    "RecordingAuditSink",
    "AuditRecord",
    "PlainTextHasher",
    # Contextos
    "authenticated_context",
    "impersonated_context",
    "system_context",
    "make_user",
    # Cableado
    "configure_test_identity",
    "create_test_user",
    "TEST_SECRET_KEY",
]
