"""
Los proveedores OAuth: el descriptor y los preconfigurados.

Un `OAuthProvider` es **datos y una función**: las tres URLs, el client, los scopes, y cómo se lee
el perfil que devuelve el proveedor. Nada más. No hay una clase por proveedor con métodos
sobreescribibles, y eso es deliberado: lo único que varía de verdad entre Google y GitHub es la
forma del JSON del `userinfo`, así que una jerarquía de clases sería una ceremonia alrededor de
una función de mapeo.

Los preconfigurados son **funciones y no constantes** porque necesitan `client_id` y
`client_secret`: una constante con los campos vacíos invita a olvidarse de llenar uno, y un
proveedor sin secreto falla recién en el intercambio de código, contra el servidor del tercero.
"""
from __future__ import annotations

import typing as t

from pydantic import BaseModel, Field, SecretStr, field_validator

__all__ = [
    "OAuthProfile",
    "OAuthProvider",
    "ProfileParser",
    "google",
    "github",
    "microsoft",
    "gitlab",
    "discord",
    "PROVIDER_FACTORIES",
]


class OAuthProfile(BaseModel):
    """
    Lo que se saca del `userinfo` de un proveedor, normalizado.

    `email_verified` es el campo que decide si se puede vincular por mail, y por eso su default
    es **`False`**: un proveedor que no lo informa se trata como "no verificado". Al revés
    —asumir verificado cuando falta— es exactamente el agujero de toma de cuentas que este
    plugin existe para no tener: cualquiera puede registrar una cuenta en un IdP permisivo con el
    mail de otro.
    """

    account_id: str
    email: str | None = None
    email_verified: bool = False
    name: str | None = None
    image: str | None = None
    raw: dict[str, t.Any] = Field(default_factory=dict)


#: Cómo se lee el JSON del `userinfo` de un proveedor.
ProfileParser = t.Callable[[dict[str, t.Any]], OAuthProfile]


class OAuthProvider(BaseModel):
    """
    Un proveedor OAuth 2.0 / OIDC configurado.

    Args:
        id: El `provider_id` con el que se guarda en `account`. **Es parte de la clave única
            junto con el id de la cuenta**, así que cambiarlo desvincula a todos los usuarios de
            ese proveedor.
        authorize_url, token_url, userinfo_url: Los tres endpoints. No hay descubrimiento
            automático por `.well-known`: sería una llamada de red en el arranque —o peor, en el
            primer login— contra un servicio de un tercero, y las URLs de los proveedores
            grandes no cambian.
        scopes: Lo que se pide. Los defaults piden **el mínimo para identificar** y nada más:
            un scope de más es un permiso que el usuario ve en la pantalla de consentimiento y
            que la aplicación no necesita.
        parse_profile: Cómo se lee el `userinfo`.
    """

    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    id: str
    client_id: str
    client_secret: SecretStr
    authorize_url: str
    token_url: str
    userinfo_url: str
    scopes: tuple[str, ...] = ()
    parse_profile: ProfileParser

    #: Parámetros extra para la URL de autorización. Google necesita `access_type=offline` para
    #: devolver un refresh token, y sin eso el token guardado no se puede renovar nunca.
    extra_authorize_params: dict[str, str] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_no_vacio(cls, valor: str) -> str:
        if not valor.strip():
            raise ValueError("El `id` del proveedor no puede estar vacío.")
        return valor

    @field_validator("authorize_url", "token_url", "userinfo_url")
    @classmethod
    def _https(cls, valor: str) -> str:
        """
        Exige HTTPS.

        Por `http://` viajarían en claro el `code` y el `client_secret`. Se rechaza acá y no en
        el uso porque es un error de configuración, y el lugar para descubrirlo es el arranque.
        `localhost` se permite para poder correr un proveedor falso en un test o en desarrollo.
        """
        if valor.startswith("https://"):
            return valor
        if valor.startswith("http://localhost") or valor.startswith("http://127.0.0.1"):
            return valor
        raise ValueError(
            f"Las URLs de un proveedor OAuth tienen que ser HTTPS: {valor!r}. Por HTTP "
            f"viajarían en claro el código de autorización y el secreto del cliente."
        )


# ── Los parsers ───────────────────────────────────────────────────────────────
def _perfil_oidc(datos: dict[str, t.Any]) -> OAuthProfile:
    """
    El `userinfo` estándar de OIDC: Google, Microsoft, GitLab y cualquiera que cumpla la spec.

    `email_verified` puede venir como booleano o como el string `"true"` —los proveedores no se
    ponen de acuerdo— así que se normaliza en vez de confiar en el tipo.
    """
    verificado = datos.get("email_verified", datos.get("verified_email", False))
    return OAuthProfile(
        account_id=str(datos.get("sub") or datos.get("id") or ""),
        email=datos.get("email"),
        email_verified=verificado is True or verificado == "true",
        name=datos.get("name") or datos.get("preferred_username"),
        image=datos.get("picture") or datos.get("avatar_url"),
        raw=datos,
    )


def _perfil_github(datos: dict[str, t.Any]) -> OAuthProfile:
    """
    GitHub. No es OIDC: el `userinfo` es `/user` y usa `id` y `avatar_url`.

    ⚠️ **`/user` no dice si el mail está verificado, y el `email` que trae puede ser `null`** si
    el usuario lo tiene privado. Se marca `email_verified=False` siempre: quien quiera vincular
    por mail con GitHub tiene que pegarle a `/user/emails` —que requiere el scope
    `user:email`— y decidir con eso. Asumirlo verificado sería tomar la palabra de un campo que
    el proveedor no informa.
    """
    return OAuthProfile(
        account_id=str(datos.get("id") or ""),
        email=datos.get("email"),
        email_verified=False,
        name=datos.get("name") or datos.get("login"),
        image=datos.get("avatar_url"),
        raw=datos,
    )


def _perfil_discord(datos: dict[str, t.Any]) -> OAuthProfile:
    """Discord: `verified` en vez de `email_verified`, y el avatar se arma con dos campos."""
    avatar = datos.get("avatar")
    identificador = str(datos.get("id") or "")
    return OAuthProfile(
        account_id=identificador,
        email=datos.get("email"),
        email_verified=bool(datos.get("verified", False)),
        name=datos.get("global_name") or datos.get("username"),
        image=(
            f"https://cdn.discordapp.com/avatars/{identificador}/{avatar}.png"
            if avatar
            else None
        ),
        raw=datos,
    )


# ── Los preconfigurados ───────────────────────────────────────────────────────
def google(*, client_id: str, client_secret: str, **extra: t.Any) -> OAuthProvider:
    """
    Google, vía OIDC.

    `access_type=offline` va por default: sin eso Google **no devuelve refresh token**, y el
    access token guardado deja de servir en una hora sin forma de renovarlo. Es el detalle que
    hace que la integración parezca funcionar en el test y falle al día siguiente.
    """
    return OAuthProvider(
        id="google",
        client_id=client_id,
        client_secret=SecretStr(client_secret),
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
        scopes=("openid", "email", "profile"),
        parse_profile=_perfil_oidc,
        extra_authorize_params={"access_type": "offline"},
        **extra,
    )


def github(*, client_id: str, client_secret: str, **extra: t.Any) -> OAuthProvider:
    """GitHub. Ver `_perfil_github` para la advertencia sobre el mail."""
    return OAuthProvider(
        id="github",
        client_id=client_id,
        client_secret=SecretStr(client_secret),
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        userinfo_url="https://api.github.com/user",
        scopes=("read:user", "user:email"),
        parse_profile=_perfil_github,
        **extra,
    )


def microsoft(
    *, client_id: str, client_secret: str, tenant: str = "common", **extra: t.Any
) -> OAuthProvider:
    """
    Microsoft Entra ID.

    `tenant="common"` acepta cuentas personales y de cualquier organización. ⚠️ Si tu aplicación
    es de una sola organización, **poné su tenant**: con `common`, cualquier cuenta de Microsoft
    del mundo pasa la autenticación, y filtrar por dominio de mail después es una comprobación
    que se olvida.
    """
    return OAuthProvider(
        id="microsoft",
        client_id=client_id,
        client_secret=SecretStr(client_secret),
        authorize_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        token_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        userinfo_url="https://graph.microsoft.com/oidc/userinfo",
        scopes=("openid", "email", "profile"),
        parse_profile=_perfil_oidc,
        **extra,
    )


def gitlab(
    *,
    client_id: str,
    client_secret: str,
    base_url: str = "https://gitlab.com",
    **extra: t.Any,
) -> OAuthProvider:
    """GitLab, vía OIDC. `base_url` para una instancia autohospedada."""
    return OAuthProvider(
        id="gitlab",
        client_id=client_id,
        client_secret=SecretStr(client_secret),
        authorize_url=f"{base_url}/oauth/authorize",
        token_url=f"{base_url}/oauth/token",
        userinfo_url=f"{base_url}/oauth/userinfo",
        scopes=("openid", "email", "profile"),
        parse_profile=_perfil_oidc,
        **extra,
    )


def discord(*, client_id: str, client_secret: str, **extra: t.Any) -> OAuthProvider:
    """Discord."""
    return OAuthProvider(
        id="discord",
        client_id=client_id,
        client_secret=SecretStr(client_secret),
        authorize_url="https://discord.com/oauth2/authorize",
        token_url="https://discord.com/api/oauth2/token",
        userinfo_url="https://discord.com/api/users/@me",
        scopes=("identify", "email"),
        parse_profile=_perfil_discord,
        **extra,
    )


#: Los preconfigurados por nombre, para armarlos desde configuración.
PROVIDER_FACTORIES: dict[str, t.Callable[..., OAuthProvider]] = {
    "google": google,
    "github": github,
    "microsoft": microsoft,
    "gitlab": gitlab,
    "discord": discord,
}
