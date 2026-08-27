"""
Los flujos de `oauth`: iniciar, callback, vincular y desvincular.

Authorization Code con **PKCE obligatorio**, `state` de un solo uso guardado del lado del
servidor, y una política de vinculación que por default **no vincula por mail**.

Esa última es la decisión que más importa de todo el plugin, así que va acá arriba:

⚠️ **Vincular automáticamente porque el mail coincide es la toma de cuentas más común de OAuth.**
Si una cuenta local tiene `ana@ejemplo.com` y el flujo trae una identidad de proveedor con ese
mismo mail, vincularlas deja que cualquiera que consiga registrar `ana@ejemplo.com` en *cualquier*
IdP configurado entre a la cuenta de Ana. Y hay IdPs que no verifican el mail, o que permiten
cambiarlo sin re-verificar. Por eso el default es `LinkPolicy.NEVER`: la coincidencia produce un
`OAuthAccountNotLinkedError` que le dice al usuario que inicie sesión con su método actual y
vincule desde ahí. La vinculación explícita es la única segura.

`LinkPolicy.VERIFIED_EMAIL` existe porque hay despliegues donde todos los proveedores son de
confianza (un único IdP corporativo), y ahí la fricción no compra nada. Exige que el proveedor
informe el mail como verificado **y** que la cuenta local lo tenga verificado: las dos, porque
cada una sola deja una mitad del agujero abierta.
"""
from __future__ import annotations

import enum
import typing as t
from datetime import timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

from hexcore.darwin.domain.entities import Account, User
from hexcore.darwin.domain.value_objects import Email
from hexcore.darwin.plugins.oauth.domain import (
    OAuthAccountAlreadyLinkedError,
    OAuthAccountNotLinkedError,
    OAuthEmailNotVerifiedError,
    OAuthProviderNotConfiguredError,
    OAuthStateError,
    generate_pkce_verifier,
    pkce_challenge,
    CODE_CHALLENGE_METHOD,
    OAuthState,
)

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.context import Transport
    from hexcore.darwin.domain.entities import IdentitySession
    from hexcore.darwin.domain.ports import (
        AbstractAccountRepository,
        AbstractClock,
        AbstractUserRepository,
    )
    from hexcore.darwin.domain.value_objects import TokenPair
    from hexcore.darwin.infrastructure.secretbox import SecretBox
    from hexcore.darwin.plugins.oauth.domain import (
        AbstractOAuthHttpClient,
        AbstractOAuthStateRepository,
    )
    from hexcore.darwin.plugins.oauth.providers import OAuthProfile, OAuthProvider

__all__ = [
    "LinkPolicy",
    "DEFAULT_STATE_TTL",
    "Authorization",
    "OAuthSignIn",
    "OAuthService",
]


class LinkPolicy(enum.StrEnum):
    """
    Cuándo se vincula una identidad de proveedor a una cuenta local existente.

    - `NEVER` (**default**): nunca por coincidencia de mail. La única vinculación es la explícita
      —el usuario ya autenticado inicia un flujo de vinculación— y una coincidencia produce un
      `OAuthAccountNotLinkedError` que le explica qué hacer.
    - `VERIFIED_EMAIL`: se vincula si el proveedor informa el mail verificado **y** la cuenta
      local lo tiene verificado. Para despliegues con un único IdP de confianza.
    - `ANY_EMAIL`: se vincula por coincidencia de mail, sin más. ⚠️ **Es la toma de cuentas del
      docstring del módulo.** Existe sólo porque hay migraciones desde sistemas que ya lo hacían
      y cortarlo de golpe deja usuarios afuera; ponelo con fecha de vencimiento.
    """

    NEVER = "never"
    VERIFIED_EMAIL = "verified_email"
    ANY_EMAIL = "any_email"


#: Cuánto vive un `state`.
#:
#: 10 minutos: lo que puede tardar alguien en leer la pantalla de consentimiento, crear la cuenta
#: en el proveedor si no la tenía, y resolver un segundo factor del lado del proveedor. Más largo
#: sería una ventana en la que un `state` filtrado sigue sirviendo.
DEFAULT_STATE_TTL = timedelta(minutes=10)


class Authorization(t.NamedTuple):
    """A dónde mandar al usuario, y el `state` que se emitió."""

    url: str
    state: str


class OAuthSignIn(t.NamedTuple):
    """
    El resultado de un callback.

    `created` distingue "se creó una cuenta nueva" de "entró a la que ya tenía", que es la
    diferencia entre mandar un mail de bienvenida y no mandarlo.
    """

    user: "User"
    session: "IdentitySession"
    tokens: "TokenPair"
    created: bool


class OAuthService:
    """
    Los flujos de OAuth.

    Uso::

        servicio = get_oauth_service()
        autorizacion = await servicio.start("google", redirect_uri="https://app/cb")
        # ...el usuario vuelve con `code` y `state`
        entrada = await servicio.callback(
            "google", code=code, state=state, redirect_uri="https://app/cb"
        )
    """

    def __init__(
        self,
        *,
        providers: t.Mapping[str, "OAuthProvider"],
        states: "AbstractOAuthStateRepository",
        users: "AbstractUserRepository",
        accounts: "AbstractAccountRepository",
        sessions: t.Any,
        clock: "AbstractClock",
        http: "AbstractOAuthHttpClient",
        secrets_box: "SecretBox",
        allowed_redirect_uris: t.Sequence[str] = (),
        link_policy: LinkPolicy = LinkPolicy.NEVER,
        state_ttl: timedelta = DEFAULT_STATE_TTL,
    ) -> None:
        self._providers = dict(providers)
        self._states = states
        self._users = users
        self._accounts = accounts
        self._sessions = sessions
        self._clock = clock
        self._http = http
        self._box = secrets_box
        self._redirects = tuple(allowed_redirect_uris)
        self._policy = link_policy
        self._ttl = state_ttl

    # ── Iniciar ───────────────────────────────────────────────────────────────
    def provider(self, provider_id: str) -> "OAuthProvider":
        """
        El proveedor configurado.

        Raises:
            OAuthProviderNotConfiguredError: no está. 404, para no confirmarle a quien enumera
                cuáles sí están configurados.
        """
        proveedor = self._providers.get(provider_id)
        if proveedor is None:
            raise OAuthProviderNotConfiguredError(
                f"El proveedor {provider_id!r} no está configurado."
            )
        return proveedor

    @property
    def provider_ids(self) -> tuple[str, ...]:
        """Los proveedores configurados. Es lo que la interfaz necesita para dibujar botones."""
        return tuple(self._providers)

    async def start(
        self,
        provider_id: str,
        *,
        redirect_uri: str,
        link_user_id: UUID | None = None,
        extra_params: t.Mapping[str, str] | None = None,
    ) -> Authorization:
        """
        Emite el `state`, guarda el verificador de PKCE y devuelve la URL de autorización.

        Args:
            provider_id: Cuál proveedor.
            redirect_uri: A dónde vuelve el usuario. **Se valida contra la allowlist**: sin eso,
                un atacante inicia el flujo con su propia URI y se lleva el `code` de la víctima
                — el proveedor lo redirige a donde le digan si la URI está registrada con un
                comodín, y muchas lo están.
            link_user_id: Si el flujo es de vinculación, a qué usuario. Se fija **acá** y no se
                lee del callback: el callback lo controla en parte quien maneja el navegador.
            extra_params: Parámetros extra para la URL (`prompt`, `login_hint`, …).

        Raises:
            OAuthProviderNotConfiguredError, ValueError: la URI no está en la allowlist.
        """
        proveedor = self.provider(provider_id)
        self._validar_redirect(redirect_uri)

        from hexcore.darwin.infrastructure.hashing import generate_token, hash_token

        estado = generate_token()
        verificador = generate_pkce_verifier()
        ahora = self._clock.now()

        await self._states.add(
            OAuthState(
                id=uuid4(),
                provider_id=provider_id,
                state_hash=hash_token(estado),
                code_verifier_encrypted=self._box.encrypt(verificador),
                redirect_uri=redirect_uri,
                link_user_id=link_user_id,
                expires_at=ahora + self._ttl,
            )
        )

        parametros: dict[str, str] = {
            "response_type": "code",
            "client_id": proveedor.client_id,
            "redirect_uri": redirect_uri,
            "state": estado,
            "code_challenge": pkce_challenge(verificador),
            "code_challenge_method": CODE_CHALLENGE_METHOD,
            **proveedor.extra_authorize_params,
            **dict(extra_params or {}),
        }
        if proveedor.scopes:
            parametros["scope"] = " ".join(proveedor.scopes)

        separador = "&" if "?" in proveedor.authorize_url else "?"
        return Authorization(
            url=f"{proveedor.authorize_url}{separador}{urlencode(parametros)}",
            state=estado,
        )

    # ── El callback ───────────────────────────────────────────────────────────
    async def callback(
        self,
        provider_id: str,
        *,
        code: str,
        state: str,
        redirect_uri: str,
        transport: "Transport" = "cookie",
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> OAuthSignIn:
        """
        Canjea el código, resuelve a qué usuario corresponde y abre la sesión.

        El orden es deliberado: primero se consume el `state` —así un `state` inválido no gasta
        una llamada al proveedor— y sólo después se habla con él.

        Raises:
            OAuthStateError: el `state` no existe, venció, ya se usó, o el `redirect_uri` no
                coincide con el del inicio.
            OAuthExchangeError: el proveedor rechazó el canje.
            OAuthAccountNotLinkedError: hay una cuenta local con ese mail sin vincular, y la
                política no permite vincular sola.
            OAuthAccountAlreadyLinkedError: en un flujo de vinculación, esa identidad ya está en
                otra cuenta.
        """
        proveedor = self.provider(provider_id)
        ahora = self._clock.now()

        from hexcore.darwin.infrastructure.hashing import hash_token

        guardado = await self._states.consume(
            provider_id, hash_token(state), at=ahora
        )
        if guardado is None:
            raise OAuthStateError(
                "El `state` del flujo no es válido, venció o ya se usó. Volvé a empezar."
            )

        # El `redirect_uri` del callback tiene que ser el mismo con el que se inició. El
        # proveedor ya lo valida contra el suyo, pero eso no cubre el caso de dos URIs ambas
        # registradas: sin este chequeo, un flujo iniciado para una se puede completar en la
        # otra.
        if guardado.redirect_uri != redirect_uri:
            raise OAuthStateError(
                "El `redirect_uri` del callback no coincide con el del inicio del flujo."
            )

        tokens = await self._http.exchange_code(
            proveedor.token_url,
            code=code,
            redirect_uri=redirect_uri,
            client_id=proveedor.client_id,
            client_secret=proveedor.client_secret.get_secret_value(),
            code_verifier=self._box.decrypt(guardado.code_verifier_encrypted),
        )

        # El perfil sale del `userinfo` y **no** del `id_token`. Verificar un `id_token` bien
        # exige traer y cachear el JWKS de cada proveedor y validar `iss`/`aud`/firma; usarlo
        # sin verificar es peor que no usarlo, porque el token viene del mismo canal que el
        # atacante controlaría. El `userinfo` da lo mismo sobre un canal ya autenticado con el
        # access token, y ese access token lo emitió el proveedor recién.
        crudo = await self._http.fetch_profile(
            proveedor.userinfo_url, access_token=tokens.access_token
        )
        perfil = proveedor.parse_profile(crudo)
        if not perfil.account_id:
            raise OAuthStateError(
                "El proveedor no devolvió un identificador de cuenta en el perfil."
            )

        if guardado.link_user_id is not None:
            usuario = await self._vincular(
                guardado.link_user_id, proveedor, perfil, tokens
            )
            creado = False
        else:
            usuario, creado = await self._resolver_usuario(proveedor, perfil, tokens)

        sesion, par = await self._sessions.create(
            actor=usuario,
            transport=transport,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return OAuthSignIn(user=usuario, session=sesion, tokens=par, created=creado)

    # ── Vincular y desvincular ────────────────────────────────────────────────
    async def unlink(self, *, user_id: UUID, provider_id: str) -> None:
        """
        Desvincula un proveedor.

        ⚠️ **Se niega a dejar la cuenta sin ningún método de acceso.** Desvincular el único
        proveedor de un usuario que no tiene contraseña lo deja afuera de su propia cuenta, y el
        botón que hace eso está a un click de distancia en cualquier pantalla de ajustes.

        Raises:
            OAuthProviderNotConfiguredError: no hay una cuenta de ese proveedor para el usuario.
            ValueError: es el último método de acceso.
        """
        cuentas = await self._accounts.list_for_user(user_id)
        objetivo = next((c for c in cuentas if c.provider_id == provider_id), None)
        if objetivo is None:
            raise OAuthProviderNotConfiguredError(
                f"No hay una cuenta de {provider_id!r} vinculada a este usuario."
            )

        restantes = [
            c
            for c in cuentas
            if c.id != objetivo.id and (c.is_credential and c.password or not c.is_credential)
        ]
        if not restantes:
            raise ValueError(
                "No se puede desvincular el único método de acceso de la cuenta: quedaría sin "
                "forma de iniciar sesión. Poné una contraseña o vinculá otro proveedor primero."
            )

        await self._accounts.delete(objetivo.id)

    async def list_linked(self, user_id: UUID) -> list[str]:
        """Los `provider_id` vinculados, sin la credencial local."""
        cuentas = await self._accounts.list_for_user(user_id)
        return [c.provider_id for c in cuentas if not c.is_credential]

    # ── Interno ───────────────────────────────────────────────────────────────
    def _validar_redirect(self, redirect_uri: str) -> None:
        """
        Valida contra la allowlist.

        Con la lista vacía **no se valida**, y eso es a propósito para que un test o un
        desarrollo local no tengan que declararla. En producción se declara: ver el `Args` de
        `start`.
        """
        if not self._redirects:
            return
        if redirect_uri not in self._redirects:
            raise ValueError(
                f"El `redirect_uri` {redirect_uri!r} no está en la lista de URIs permitidas. "
                f"Agregalo a `allowed_redirect_uris` si es legítimo."
            )

    async def _resolver_usuario(
        self,
        proveedor: "OAuthProvider",
        perfil: "OAuthProfile",
        tokens: t.Any,
    ) -> tuple["User", bool]:
        """
        A qué usuario corresponde esta identidad. Tres caminos, en este orden:

        1. **La identidad ya está vinculada** → ese usuario. Se refrescan los tokens guardados.
        2. **No hay cuenta local con ese mail** → se crea el usuario y la cuenta.
        3. **Hay una cuenta local con ese mail pero sin vincular** → decide `link_policy`. Ver el
           docstring del módulo: por default, **no se vincula**.
        """
        existente = await self._accounts.get_by_provider(
            proveedor.id, perfil.account_id
        )
        if existente is not None:
            await self._accounts.update(
                existente.model_copy(update=self._campos_de_token(tokens, perfil))
            )
            usuario = await self._users.get_by_id(existente.user_id)
            if usuario is None:
                # La cuenta quedó apuntando a un usuario borrado. Es un estado inconsistente
                # —la FK es `CASCADE`— así que no se intenta arreglar en silencio.
                raise OAuthStateError(
                    "La cuenta del proveedor apunta a un usuario que no existe."
                )
            return usuario, False

        mail = Email(value=perfil.email).value if perfil.email else None
        local = await self._users.get_by_email(mail) if mail else None

        if local is None:
            return await self._crear_usuario(proveedor, perfil, tokens, mail), True

        self._autorizar_vinculacion_automatica(proveedor, perfil, local)
        await self._accounts.add(
            self._cuenta_nueva(local.id, proveedor, perfil, tokens)
        )
        return local, False

    def _autorizar_vinculacion_automatica(
        self, proveedor: "OAuthProvider", perfil: "OAuthProfile", local: "User"
    ) -> None:
        """
        Decide si se puede vincular por coincidencia de mail. Ver el docstring del módulo.
        """
        if self._policy is LinkPolicy.ANY_EMAIL:
            return

        if self._policy is LinkPolicy.NEVER:
            raise OAuthAccountNotLinkedError(
                f"Ya existe una cuenta con ese correo. Iniciá sesión con tu método actual y "
                f"vinculá {proveedor.id!r} desde los ajustes de tu cuenta. No se vincula sola "
                f"por seguridad: si lo hiciera, cualquiera que registre ese correo en un "
                f"proveedor podría entrar a tu cuenta."
            )

        # VERIFIED_EMAIL: las dos verificaciones, no una.
        if not perfil.email_verified:
            raise OAuthEmailNotVerifiedError(
                f"{proveedor.id!r} no informa ese correo como verificado, así que no se puede "
                f"vincular automáticamente a una cuenta existente."
            )
        if not local.email_verified:
            raise OAuthAccountNotLinkedError(
                "La cuenta local todavía no verificó su correo. Verificalo e iniciá sesión para "
                "vincular el proveedor."
            )

    async def _vincular(
        self,
        user_id: UUID,
        proveedor: "OAuthProvider",
        perfil: "OAuthProfile",
        tokens: t.Any,
    ) -> "User":
        """
        El flujo explícito de vinculación: el usuario ya autenticado suma un proveedor.

        Raises:
            OAuthAccountAlreadyLinkedError: la identidad ya está en otra cuenta. No se mueve: la
                primera cuenta perdería su método de acceso, y si era el único, el acceso.
        """
        existente = await self._accounts.get_by_provider(
            proveedor.id, perfil.account_id
        )
        if existente is not None and existente.user_id != user_id:
            raise OAuthAccountAlreadyLinkedError(
                f"Esa cuenta de {proveedor.id!r} ya está vinculada a otro usuario."
            )

        usuario = await self._users.get_by_id(user_id)
        if usuario is None:
            raise OAuthStateError("El usuario del flujo de vinculación no existe.")

        if existente is None:
            await self._accounts.add(
                self._cuenta_nueva(user_id, proveedor, perfil, tokens)
            )
        else:
            await self._accounts.update(
                existente.model_copy(update=self._campos_de_token(tokens, perfil))
            )
        return usuario

    async def _crear_usuario(
        self,
        proveedor: "OAuthProvider",
        perfil: "OAuthProfile",
        tokens: t.Any,
        mail: str | None,
    ) -> "User":
        """
        Crea el usuario y su cuenta del proveedor.

        **`email_verified` se copia del proveedor y no se asume `True`.** Un usuario creado por
        OAuth con el mail marcado verificado sin que el proveedor lo diga es una afirmación que
        nadie comprobó, y de la que después dependen otros flujos —el reset de contraseña, por
        ejemplo—.
        """
        usuario = await self._users.add(
            User(
                email=mail or f"{proveedor.id}:{perfil.account_id}",
                email_verified=perfil.email_verified,
                name=perfil.name,
                image=perfil.image,
            )
        )
        await self._accounts.add(
            self._cuenta_nueva(usuario.id, proveedor, perfil, tokens)
        )
        return usuario

    def _cuenta_nueva(
        self,
        user_id: UUID,
        proveedor: "OAuthProvider",
        perfil: "OAuthProfile",
        tokens: t.Any,
    ) -> Account:
        return Account(
            user_id=user_id,
            provider_id=proveedor.id,
            account_id=perfil.account_id,
            **self._campos_de_token(tokens, perfil),
        )

    def _campos_de_token(
        self, tokens: t.Any, perfil: "OAuthProfile"
    ) -> dict[str, t.Any]:
        """
        Los campos de token, **cifrados**.

        Son credenciales de otro sistema: un dump que las entregue en claro es un incidente en la
        API del tercero además del propio, y el usuario ni se enteraría de que su cuenta de
        Google quedó expuesta por una base nuestra.
        """
        ahora = self._clock.now()
        return {
            "access_token": self._box.encrypt(tokens.access_token),
            "refresh_token": self._box.encrypt_optional(tokens.refresh_token),
            "id_token": self._box.encrypt_optional(tokens.id_token),
            "scope": tokens.scope,
            "access_token_expires_at": (
                ahora + timedelta(seconds=tokens.expires_in)
                if tokens.expires_in
                else None
            ),
            "refresh_token_expires_at": (
                ahora + timedelta(seconds=tokens.refresh_expires_in)
                if tokens.refresh_expires_in
                else None
            ),
        }

    def decrypt_access_token(self, account: Account) -> str | None:
        """
        El access token del proveedor, en claro, para llamar a su API.

        Es el único camino de salida: los tokens se guardan cifrados, así que leer la columna
        directo no sirve. Se expone acá —y no se descifra al hidratar— para que el valor en claro
        exista sólo en la línea que lo usa y no en cada `repr` de una entidad.
        """
        return self._box.decrypt_optional(account.access_token)

    def decrypt_refresh_token(self, account: Account) -> str | None:
        """El refresh token del proveedor, en claro. Ver `decrypt_access_token`."""
        return self._box.decrypt_optional(account.refresh_token)
