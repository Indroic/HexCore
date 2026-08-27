"""
Configuración de Darwin. Se cuelga de `ServerConfig.darwin`.

Mismo precedente que `ServerConfig.cqrs`: campo opcional, `None` = deshabilitado, sin impacto
en los módulos existentes.

**La clave de firma no tiene default, y eso es la decisión de seguridad más importante del
módulo.** Todo campo de `ServerConfig` tiene uno, y un secreto de firma con default es lo peor
que puede shippear una librería de auth: la mitad de los despliegues quedaría firmando con el
mismo valor de ejemplo, y quien lea el código fuente puede forjar tokens para todos ellos. Acá
se lee de `HEXCORE_DARWIN_SECRET_KEY` y, si falta con `debug=False`, **no arranca**.

Módulo de aplicación pero **sin dependencias de infraestructura**: sólo stdlib y pydantic. Lo
importa `hexcore.config`, que a su vez lo importa medio framework, así que no puede arrastrar
sqlalchemy ni joserfc.
"""
from __future__ import annotations

import os
import typing as t
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

__all__ = [
    "CookieConfig",
    "TokenConfig",
    "PasswordPolicy",
    "IdentityConfig",
    "SECRET_KEY_ENV",
]

#: De dónde se lee la clave de firma. Variable de entorno y no campo de config con default:
#: ver el docstring del módulo.
SECRET_KEY_ENV = "HEXCORE_DARWIN_SECRET_KEY"

#: De dónde se lee el backend de almacenamiento si no se declara en la config.
#:
#: Existe porque el backend es una decisión de **despliegue** y no de código: la misma imagen
#: puede correr contra Postgres en producción y contra Mongo en un entorno de pruebas, y obligar
#: a recompilar la config para eso sería absurdo.
STORAGE_ENV = "HEXCORE_DARWIN_STORAGE"

#: Largo mínimo del secreto, en caracteres. 32 no es arbitrario: por debajo de ~256 bits de
#: entropía, un HMAC-SHA256 se puede atacar por fuerza bruta con hardware alquilado.
MIN_SECRET_LENGTH = 32


class CookieConfig(BaseModel):
    """
    Atributos de las cookies de sesión. Los defaults son los seguros.

    El prefijo `__Host-` es el más restrictivo que existe y por eso es el default: el navegador
    sólo acepta una cookie así si viene por HTTPS, con `Path=/` y **sin** `Domain`. Ese último
    punto es el que importa: sin `Domain`, un subdominio comprometido no puede escribir la
    cookie de sesión del dominio principal — que es el ataque que `SameSite` no cubre.

    Por eso `domain` no es configurable: declararlo desactivaría el prefijo, y ofrecerlo como
    opción sería ofrecer la forma insegura al mismo nivel que la segura.
    """

    model_config = ConfigDict(frozen=True)

    #: Nombre de la cookie del access token. El prefijo se agrega solo si `secure`.
    access_name: str = "session"
    refresh_name: str = "refresh"
    csrf_name: str = "csrf"

    #: `Secure` + prefijo `__Host-`. Se apaga **sólo** para desarrollo sobre HTTP.
    secure: bool = True
    http_only: bool = True

    #: `Lax` y no `Strict`: con `Strict`, volver al sitio desde un link externo llega sin
    #: cookie y el usuario ve un logout que no pidió. `Lax` cubre el CSRF de los métodos que
    #: cambian estado, que es lo que importa, y el resto lo cubre el chequeo explícito.
    same_site: t.Literal["lax", "strict", "none"] = "lax"
    path: str = "/"

    def name_for(self, kind: t.Literal["access", "refresh", "csrf"]) -> str:
        """
        El nombre real de la cookie, con prefijo si corresponde.

        El prefijo `__Host-` se agrega sólo con `secure=True`: en HTTP el navegador rechazaría
        la cookie entera, y en desarrollo eso se traduce en "no puedo loguearme y no sé por
        qué".
        """
        base = {
            "access": self.access_name,
            "refresh": self.refresh_name,
            "csrf": self.csrf_name,
        }[kind]
        return f"__Host-{base}" if self.secure else base


class TokenConfig(BaseModel):
    """Vidas y algoritmos de los tokens."""

    model_config = ConfigDict(frozen=True)

    #: Quién emite. Va en el claim `iss` y se verifica.
    issuer: str = "hexcore"

    #: 120 s. Es el número que hace viable la revocación en tres capas: con un `exp` así de
    #: corto, el camino caliente no consulta la base porque el peor caso de un token revocado
    #: que sigue sirviendo son dos minutos. Subirlo a una hora convierte la revocación en algo
    #: que hay que consultar por request.
    access_ttl: timedelta = timedelta(seconds=120)
    refresh_ttl: timedelta = timedelta(days=30)

    #: Techo de la sesión, independiente del refresh. Sin esto, rotar el refresh indefinidamente
    #: es una sesión eterna, y "cerrá sesión en todos los dispositivos" nunca termina de valer.
    session_ttl: timedelta = timedelta(days=90)

    #: `Ed25519`, no el `EdDSA` genérico que RFC 9864 deprecó. Ver `infrastructure/keys.py`.
    algorithm: str = "Ed25519"

    #: Tolerancia de desfase de reloj entre el emisor y el verificador. Se aplica **sólo** a la
    #: ventana temporal, nunca a la revocación: un margen ahí sería una ventana de uso para un
    #: token ya revocado.
    leeway: timedelta = timedelta(seconds=30)

    @model_validator(mode="after")
    def _las_vidas_son_coherentes(self) -> "TokenConfig":
        if self.access_ttl >= self.refresh_ttl:
            raise ValueError(
                "TokenConfig.access_ttl tiene que ser menor que refresh_ttl. Si el access "
                "vive tanto como el refresh, rotar no sirve para nada y la revocación pierde "
                "su capa más barata."
            )
        if self.refresh_ttl > self.session_ttl:
            raise ValueError(
                "TokenConfig.refresh_ttl no puede exceder session_ttl: el refresh sobreviviría "
                "al techo de la sesión y 'cerrar sesión en todos los dispositivos' no valdría."
            )
        return self


class PasswordPolicy(BaseModel):
    """
    Política de contraseñas. Deliberadamente mínima.

    **Sólo largo mínimo, sin reglas de composición.** No es pereza: exigir mayúsculas, dígitos y
    símbolos empuja a `Password1!` —que es corta y está en todos los diccionarios— y no a una
    passphrase larga. Es la recomendación del NIST SP 800-63B desde 2017: largo mínimo y
    chequeo contra contraseñas conocidas, nada de reglas de composición ni rotación forzada.

    `max_length` existe por una razón que no es de política: Argon2 hashea la entrada completa,
    así que sin techo una contraseña de 100 MB es un DoS de un request.
    """

    model_config = ConfigDict(frozen=True)

    min_length: int = 12
    max_length: int = 1024

    #: Contraseñas prohibidas explícitamente. Para inyectar una lista de las más comunes; el
    #: framework no shippea una, porque mantenerla actualizada no es su trabajo.
    denylist: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _el_minimo_es_razonable(self) -> "PasswordPolicy":
        if self.min_length < 8:
            raise ValueError(
                "PasswordPolicy.min_length no puede ser menor que 8. Por debajo de eso el "
                "espacio de búsqueda se agota con hardware de consumo, sin importar qué "
                "algoritmo de hash uses."
            )
        if self.max_length <= self.min_length:
            raise ValueError("PasswordPolicy.max_length tiene que ser mayor que min_length.")
        return self

    def validate_password(self, password: str) -> None:
        """
        Valida una contraseña contra la política.

        Raises:
            ValueError: con un mensaje que dice **qué** falta, no una lista de reglas.
        """
        if len(password) < self.min_length:
            raise ValueError(
                f"La contraseña tiene que tener al menos {self.min_length} caracteres. "
                f"Una frase larga y fácil de recordar es más segura que una corta con "
                f"símbolos."
            )
        if len(password) > self.max_length:
            raise ValueError(
                f"La contraseña no puede exceder {self.max_length} caracteres."
            )
        if password.casefold() in {p.casefold() for p in self.denylist}:
            raise ValueError(
                "Esa contraseña está en la lista de contraseñas conocidas. Elegí otra."
            )


class IdentityConfig(BaseModel):
    """
    Configuración de Darwin.

    Uso::

        # config.py del consumidor
        from hexcore.config import ServerConfig
        from hexcore.darwin import IdentityConfig

        config = ServerConfig(
            debug=False,
            allow_origins=["https://app.ejemplo.com"],
            allow_credentials=True,
            darwin=IdentityConfig(
                tokens=TokenConfig(issuer="https://api.ejemplo.com"),
            ),
        )
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    #: La clave de firma. **Sin default.** Se lee de `HEXCORE_DARWIN_SECRET_KEY` si no se pasa.
    #: `SecretStr` para que un `repr()` o un volcado de config no la imprima.
    secret_key: SecretStr | None = None

    tokens: TokenConfig = Field(default_factory=TokenConfig)
    cookies: CookieConfig = Field(default_factory=CookieConfig)
    passwords: PasswordPolicy = Field(default_factory=PasswordPolicy)

    #: El modelo de usuario concreto. `None` = el de `models.py`. Se valida al configurar el
    #: contenedor con `validate_user_model`, que rechaza un `BaseModel[T]` y una clase que no
    #: componga `UserMixin`.
    user_model: t.Any = None

    #: Dónde se guarda la identidad: `"sqlalchemy"` o `"beanie"`.
    #:
    #: `None` = detectar según qué extra esté instalado. La detección funciona cuando hay **uno
    #: solo**; con los dos instalados, el contenedor falla al arrancar y pide que se declare —
    #: elegir por una regla implícita haría que el backend dependa de qué más haya en el entorno,
    #: y el síntoma es una app que arranca contra una base vacía.
    #:
    #: ⚠️ Declaralo explícito si tu app usa `[sql]` para otra cosa y querés Mongo para la
    #: identidad, o al revés: tener el paquete instalado no significa querer guardar ahí.
    #:
    #: Se lee de `HEXCORE_DARWIN_STORAGE` si no se pasa, porque es una decisión de despliegue.
    storage: t.Literal["sqlalchemy", "beanie"] | None = None

    #: Orígenes autorizados para el chequeo anti-CSRF del transporte por cookie. Separado de
    #: `ServerConfig.allow_origins` a propósito: CORS y CSRF son controles distintos, y hacer
    #: que uno herede del otro significa que relajar CORS relaja CSRF sin que nadie lo note.
    trusted_origins: tuple[str, ...] = ()

    #: Vida del sobre firmado que cruza la cola. Un payload capturado de una dead-letter queue
    #: no se puede reproducir un mes después.
    worker_context_ttl: timedelta = timedelta(hours=24)

    #: Si el email tiene que estar verificado para poder iniciar sesión.
    require_verified_email: bool = True

    #: Techo de intentos sobre un token de verificación u OTP. Un OTP de 6 dígitos son 10^6
    #: combinaciones: sin techo se agotan en minutos.
    max_verification_attempts: int = 5

    @model_validator(mode="after")
    def _resuelve_el_almacenamiento(self) -> "IdentityConfig":
        """
        Lee `storage` del entorno si no vino en la config.

        **No valida que el backend esté instalado**: eso lo hace el contenedor, al resolver el
        primer repositorio. Acá sólo se toma el valor, porque una `IdentityConfig` se construye
        también en un proceso que no va a tocar la base —el que sólo verifica tokens, por
        ejemplo— y exigirle el extra ahí sería pedirle una dependencia que no usa.
        """
        if self.storage is None:
            del_entorno = os.getenv(STORAGE_ENV, "").strip().lower()
            if del_entorno:
                object.__setattr__(self, "storage", del_entorno)
        return self

    @model_validator(mode="after")
    def _hay_clave_de_firma(self) -> "IdentityConfig":
        """
        Resuelve la clave desde el entorno y rechaza la ausencia o un secreto débil.

        En `debug` se tolera la ausencia y se genera una efímera —de otro modo no se podría
        correr un test ni levantar la app local sin exportar una variable— pero **el secreto
        efímero cambia en cada arranque**, así que las sesiones no sobreviven un reload. Es el
        síntoma correcto: te empuja a declarar el secreto en cuanto te importa la persistencia,
        en vez de dejarte descubrir en producción que estabas firmando con un valor de ejemplo.
        """
        if self.secret_key is not None:
            self._validar_fuerza(self.secret_key.get_secret_value())
            return self

        del_entorno = os.getenv(SECRET_KEY_ENV, "").strip()
        if del_entorno:
            self._validar_fuerza(del_entorno)
            object.__setattr__(self, "secret_key", SecretStr(del_entorno))
            return self

        if not self._en_debug():
            raise ValueError(
                f"Darwin necesita una clave de firma y no hay ninguna.\n\n"
                f"Declarala en el entorno:\n\n"
                f"    export {SECRET_KEY_ENV}=\"$(python -c "
                f"'import secrets; print(secrets.token_urlsafe(48))')\"\n\n"
                f"No hay default a propósito: un secreto de firma con valor por defecto haría "
                f"que cualquiera que lea el código fuente pueda forjar tokens para todos los "
                f"despliegues que no lo cambiaron."
            )

        # En debug: efímera, y distinta en cada arranque.
        import secrets

        object.__setattr__(
            self, "secret_key", SecretStr(secrets.token_urlsafe(48))
        )
        return self

    @staticmethod
    def _validar_fuerza(secreto: str) -> None:
        if len(secreto) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"La clave de firma tiene {len(secreto)} caracteres y necesita al menos "
                f"{MIN_SECRET_LENGTH}. Generá una:\n\n"
                f"    python -c 'import secrets; print(secrets.token_urlsafe(48))'\n"
            )

    @staticmethod
    def _en_debug() -> bool:
        """
        Si la app está en modo debug.

        Se lee de `ServerConfig` con import perezoso: `hexcore.config` importa este módulo, así
        que hacerlo arriba sería un ciclo.
        """
        try:
            from hexcore.config import LazyConfig

            return bool(LazyConfig.get_config().debug)
        except Exception:
            # Si la config no se puede resolver, se asume producción: es el lado seguro.
            return False

    @model_validator(mode="after")
    def _el_csrf_no_acepta_comodin(self) -> "IdentityConfig":
        """
        `trusted_origins=["*"]` desactivaría el chequeo anti-CSRF por completo.

        Es el mismo error que el CORS con `"*"` y credenciales, un nivel más abajo: un comodín
        acá significa "cualquier origen puede mandar peticiones que cambian estado con la
        cookie de la víctima".
        """
        if "*" in self.trusted_origins:
            raise ValueError(
                "IdentityConfig.trusted_origins no acepta '*': eso desactiva el chequeo "
                "anti-CSRF y cualquier origen podría ejecutar acciones con la cookie de "
                "sesión de la víctima. Enumerá los orígenes de tu frontend."
            )
        return self
