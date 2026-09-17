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
import re
import typing as t
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

__all__ = [
    "CookieConfig",
    "TokenConfig",
    "PasswordPolicy",
    "UsernamePolicy",
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

    #: Permite bajar `min_length` por debajo de 8.
    #:
    #: El piso existe porque por debajo de 8 caracteres el espacio de búsqueda se agota con
    #: hardware de consumo, y ningún algoritmo de hash lo compensa. Pero hay casos reales donde
    #: la decisión no es de quien escribe el código —migrar un padrón viejo, un PIN numérico de
    #: un segundo factor, una normativa que fija otro mínimo— y en esos casos la alternativa a
    #: un escape hatch es forkear la política o no usarla.
    #:
    #: Es un flag aparte y no simplemente dejar bajar el número **porque tiene que costar
    #: escribirlo**: así queda grepeable, aparece en el diff, y nadie lo baja sin leer por qué
    #: estaba el piso.
    acknowledge_weak_minimum: bool = False

    @model_validator(mode="after")
    def _el_minimo_es_razonable(self) -> "PasswordPolicy":
        if self.min_length < 8 and not self.acknowledge_weak_minimum:
            raise ValueError(
                "PasswordPolicy.min_length no puede ser menor que 8. Por debajo de eso el "
                "espacio de búsqueda se agota con hardware de consumo, sin importar qué "
                "algoritmo de hash uses.\n\n"
                "Si tu caso lo exige igual —migrar un padrón viejo, un PIN de segundo factor, "
                "una normativa que fija otro mínimo— declaralo explícito:\n\n"
                f"    PasswordPolicy(min_length={self.min_length}, "
                f"acknowledge_weak_minimum=True)\n"
            )
        if self.min_length < 1:
            raise ValueError("PasswordPolicy.min_length tiene que ser al menos 1.")
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


class UsernamePolicy(BaseModel):
    """
    Política de nombres de usuario. Hermana de `PasswordPolicy`.

    Los tres parámetros que importan son **largo, forma y reservados**, y los tres son del
    consumidor: un foro quiere `pepe_1990`, un ERP quiere `APELLIDO.N`, y no hay un default que
    sirva para los dos. Lo que el framework sí decide es la **normalización**, porque de eso
    depende que `Ana` y `ana` no sean dos cuentas distintas.

    `pattern` se aplica sobre el valor **ya normalizado**, y su default no admite `@` a
    propósito: con arrobas permitidas, un username podría tener forma de mail y el resolvedor
    de identificadores del sign-in tendría que decidir cuál de los dos quiso decir el usuario.
    Cerrarlo acá hace que esa ambigüedad no exista en vez de resolverla con una heurística.

    Uso::

        UsernamePolicy(min_length=4, reserved=frozenset({"admin", "root", "api"}))
    """

    model_config = ConfigDict(frozen=True)

    min_length: int = 3
    max_length: int = 32

    #: Sobre el valor ya normalizado. Arranca con alfanumérico para que un username no pueda
    #: empezar con un punto o un guion — es lo que hace que `.admin` y `admin` no se confundan
    #: de un vistazo en un listado.
    pattern: str = r"^[a-z0-9][a-z0-9_.-]*$"

    #: Si `Ana` y `ana` son usuarios distintos. `False` por default, que es lo que espera
    #: cualquiera que tipea su nombre a mano.
    #:
    #: ⚠️ Ponerlo en `True` **no** protege de la suplantación por homoglifos, y además hace que
    #: la unicidad dependa de cómo lo tipeó cada uno. Sólo tiene sentido si tu padrón ya viene
    #: con usernames sensibles a mayúsculas y no los podés migrar.
    case_sensitive: bool = False

    #: Nombres que nadie puede tomar. Se comparan **normalizados**, así que declarar `"admin"`
    #: también bloquea `Admin` cuando `case_sensitive` es `False`.
    #:
    #: El framework no shippea una lista, por lo mismo que `PasswordPolicy.denylist`: qué
    #: nombres son sensibles depende de las rutas de cada app (`/settings`, `/api`, `/@me`…) y
    #: mantener esa lista no es trabajo del framework.
    reserved: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _los_largos_son_coherentes(self) -> "UsernamePolicy":
        if self.min_length < 1:
            raise ValueError("UsernamePolicy.min_length tiene que ser al menos 1.")
        if self.max_length < self.min_length:
            raise ValueError(
                "UsernamePolicy.max_length tiene que ser mayor o igual que min_length."
            )
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(
                f"UsernamePolicy.pattern no es una expresión regular válida: {exc}"
            ) from exc
        return self

    def normalize(self, raw: str) -> str:
        """
        El valor canónico: sin espacios alrededor y, salvo `case_sensitive`, en minúsculas.

        Es lo que se guarda y lo que se busca. Normalizar en un solo lugar es lo que impide que
        el alta use una forma y el login otra — el equivalente de lo que hace `Email`.
        """
        limpio = raw.strip()
        return limpio if self.case_sensitive else limpio.casefold()

    def validate_username(self, raw: str) -> str:
        """
        Normaliza y valida. Devuelve el valor a guardar.

        Raises:
            ValueError: con un mensaje que dice **qué** falta, no la lista de reglas.
        """
        valor = self.normalize(raw)

        if len(valor) < self.min_length:
            raise ValueError(
                f"El nombre de usuario tiene que tener al menos {self.min_length} caracteres."
            )
        if len(valor) > self.max_length:
            raise ValueError(
                f"El nombre de usuario no puede exceder {self.max_length} caracteres."
            )
        if re.match(self.pattern, valor) is None:
            raise ValueError(
                f"'{raw}' tiene caracteres que no se admiten en un nombre de usuario."
            )
        if valor in {self.normalize(r) for r in self.reserved}:
            raise ValueError("Ese nombre de usuario está reservado. Elegí otro.")
        return valor


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

    #: La política de nombres de usuario, o `None` si esta app no los usa.
    #:
    #: `None` es el default y es el comportamiento de 9.x: sin username, la única credencial de
    #: identificación es el mail. Declarar una política **habilita el campo**, no el login por
    #: username — para eso está `sign_in_identifiers`, porque "tengo usernames para mostrar en
    #: la UI pero se entra por mail" es un caso real.
    usernames: UsernamePolicy | None = None

    #: Si el alta exige una dirección de correo.
    #:
    #: `True` es el default y es 9.x. Ponerlo en `False` permite cuentas sólo con username, que
    #: es lo que habilita un padrón sin mails —empleados, alumnos, socios de un club— sin tener
    #: que inventar direcciones falsas.
    #:
    #: ⚠️ Una cuenta sin mail **no puede recuperar la contraseña ni verificar nada**: los dos
    #: flujos se apoyan en mandar un código a alguna parte. Si desactivás esto, tenés que tener
    #: otro camino de recuperación.
    require_email: bool = True

    #: Con qué se puede iniciar sesión.
    #:
    #: Explícito y no deducido de `usernames is not None`, porque son dos decisiones distintas:
    #: tener nombres de usuario y aceptarlos como credencial de login. Un foro puede mostrar
    #: `@pepe` en cada mensaje y aun así exigir el mail para entrar.
    sign_in_identifiers: tuple[t.Literal["email", "username"], ...] = ("email",)

    #: El modelo de usuario concreto. `None` = resolverlo (ver abajo). Se valida al configurar
    #: el contenedor con `validate_identity_model`, que rechaza un `BaseModel[T]` y una clase
    #: que no componga `UserMixin`.
    user_model: t.Any = None

    #: Los otros cinco modelos concretos, por simetría con `user_model`.
    #:
    #: Existen porque el modelo concreto **no se puede conseguir importando `models.py`**: ese
    #: módulo declara las seis clases sobre `Base`, así que importarlo para conseguir una sola
    #: declara también las otras cinco, y la que choca contra el modelo del consumidor rompe el
    #: arranque con un `InvalidRequestError` que apunta a la consulta, no al import. El detalle
    #: está en el docstring de `orms/sqlalchemy/registry.py`.
    #:
    #: `None` es lo normal y no hace falta tocarlos: `identity_model` encuentra la clase del
    #: consumidor buscando cuál compone el mixin. Se declaran explícitos sólo para desempatar
    #: cuando hay más de una candidata — el caso de quien mapea dos clases sobre el mismo mixin
    #: en tablas distintas, donde adivinar sería peor que preguntar.
    session_model: t.Any = None
    account_model: t.Any = None
    verification_model: t.Any = None
    audit_model: t.Any = None
    jwks_model: t.Any = None

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
    def _se_puede_identificar_a_alguien(self) -> "IdentityConfig":
        """
        Rechaza las combinaciones en las que nadie podría entrar, o entrar sería ambiguo.

        Los tres casos terminan en un despliegue roto que se descubre en el primer login, y los
        tres son un error de configuración perfectamente detectable al arrancar. Es el mismo
        criterio que `_hay_clave_de_firma`.
        """
        if not self.sign_in_identifiers:
            raise ValueError(
                "IdentityConfig.sign_in_identifiers está vacío: nadie podría iniciar sesión. "
                'Declará al menos uno: `sign_in_identifiers=("email",)`.'
            )

        if "username" in self.sign_in_identifiers and self.usernames is None:
            raise ValueError(
                "sign_in_identifiers incluye 'username' pero no hay `usernames` declarado, "
                "así que el endpoint aceptaría un identificador que ninguna política valida.\n\n"
                "    IdentityConfig(usernames=UsernamePolicy(), "
                'sign_in_identifiers=("email", "username"))\n'
            )

        if not self.require_email and self.usernames is None:
            raise ValueError(
                "require_email=False sin `usernames` declarado deja cuentas sin ningún "
                "identificador: no habría con qué darlas de alta ni con qué buscarlas.\n\n"
                "Declará la política de nombres de usuario, o dejá require_email=True."
            )

        return self

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
