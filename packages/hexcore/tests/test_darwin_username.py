"""
Login por nombre de usuario, y el mail como identificador opcional.

Hasta 10.0 la única credencial de identificación era el mail: `SignInRequest{email, password}`,
`get_by_email` como única búsqueda, y `darwin_user.email` NOT NULL. Eso deja afuera a todo
padrón que no tenga direcciones —empleados, alumnos, socios— y obliga a inventar mails falsos.

Lo que se fija acá, en orden de lo que más duele si se rompe:

1. **El señuelo se hashea igual en todas las ramas del resolvedor.** Es el invariante que
   sostiene todo el diseño del sign-in: sin él, "no existe ese usuario" responde en
   microsegundos y "contraseña equivocada" en decenas de milisegundos, y esa diferencia enumera
   cuentas sin adivinar ni una contraseña. El resolvedor de identificador agrega **ramas
   nuevas**, y cada una es una oportunidad de retornar antes de tiempo.
2. **`require_verified_email` no puede encerrar a una cuenta sin mail.** Una cuenta que nunca va
   a tener `email_verified=True` nunca podría entrar, y el síntoma sería un 403 permanente sin
   salida en un despliegue que dejó el default.
3. **Compatibilidad del cuerpo HTTP.** Un cliente de 9.x manda `{"email": ...}`.
"""
from __future__ import annotations

import pytest

pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from hexcore.darwin import (  # noqa: E402
    AbstractPasswordHasher,
    EmailNotVerifiedError,
    IdentityConfig,
    InvalidCredentialsError,
    PasswordPolicy,
    UsernameAlreadyTakenError,
    UsernamePolicy,
)
from hexcore.darwin.domain.value_objects import Username  # noqa: E402
from hexcore.darwin.testing import (  # noqa: E402
    TEST_SECRET_KEY,
    configure_test_identity,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(**extra) -> IdentityConfig:
    base = {
        "secret_key": TEST_SECRET_KEY,
        "usernames": UsernamePolicy(min_length=3),
        "sign_in_identifiers": ("email", "username"),
        "require_verified_email": False,
    }
    base.update(extra)
    return IdentityConfig(**base)  # pyright: ignore[reportArgumentType]


class HasherEspia(AbstractPasswordHasher):
    """
    Un hasher que cuenta cuántas veces se llamó a `hash`.

    Es lo que permite asertar sobre el señuelo sin medir tiempos —medir tiempos en una suite es
    escamoso y falla en CI— y sin renunciar a probar la propiedad que importa.
    """

    def __init__(self) -> None:
        self.hashes = 0

    def hash(self, password: str) -> str:
        self.hashes += 1
        return f"plano${password}"

    def verify(self, password: str, hashed: str) -> bool:
        return hashed == f"plano${password}"

    def needs_rehash(self, hashed: str) -> bool:
        return False


# ── La política ───────────────────────────────────────────────────────────────
class TestLaPoliticaDeUsername:
    def test_los_largos_son_configurables(self) -> None:
        politica = UsernamePolicy(min_length=5, max_length=8)

        with pytest.raises(ValueError, match="al menos 5"):
            politica.validate_username("abcd")
        with pytest.raises(ValueError, match="exceder 8"):
            politica.validate_username("abcdefghi")
        assert politica.validate_username("abcde") == "abcde"

    def test_normaliza_a_minusculas_y_sin_espacios_alrededor(self) -> None:
        assert UsernamePolicy().validate_username("  Pepe_1990 ") == "pepe_1990"

    def test_con_case_sensitive_respeta_las_mayusculas(self) -> None:
        politica = UsernamePolicy(case_sensitive=True, pattern=r"^[A-Za-z0-9_]+$")
        assert politica.validate_username("  Pepe ") == "Pepe"

    def test_el_patron_es_configurable(self) -> None:
        politica = UsernamePolicy(pattern=r"^[a-z]+$")
        assert politica.validate_username("pepe") == "pepe"
        with pytest.raises(ValueError, match="caracteres que no se admiten"):
            politica.validate_username("pepe99")

    def test_el_patron_por_defecto_no_admite_arroba(self) -> None:
        """
        Es lo que impide que un username tenga forma de mail. Sin esto, el resolvedor de
        identificadores tendría que adivinar cuál de los dos quiso decir el usuario.
        """
        with pytest.raises(ValueError, match="caracteres que no se admiten"):
            UsernamePolicy().validate_username("pepe@ejemplo.com")

    def test_los_reservados_se_comparan_normalizados(self) -> None:
        politica = UsernamePolicy(reserved=frozenset({"Admin"}))
        with pytest.raises(ValueError, match="reservado"):
            politica.validate_username("admin")
        with pytest.raises(ValueError, match="reservado"):
            politica.validate_username("ADMIN")

    def test_un_patron_invalido_se_rechaza_al_construir(self) -> None:
        """Al configurar y no en el primer alta: es un error de cableado."""
        with pytest.raises(ValueError, match="expresión regular"):
            UsernamePolicy(pattern="[sin-cerrar")


class TestElValueObject:
    def test_normaliza_igual_que_email(self) -> None:
        assert Username(value="  Pepe ").value == "pepe"
        assert str(Username(value="PEPE")) == "pepe"

    def test_rechaza_espacios_internos(self) -> None:
        with pytest.raises(ValueError, match="espacios"):
            Username(value="pe pe")


class TestElPisoDeLaContrasena:
    def test_por_debajo_de_ocho_hay_que_declararlo(self) -> None:
        with pytest.raises(ValueError, match="acknowledge_weak_minimum"):
            PasswordPolicy(min_length=6)

    def test_con_el_flag_se_permite(self) -> None:
        politica = PasswordPolicy(min_length=6, acknowledge_weak_minimum=True)
        politica.validate_password("123456")  # no levanta


# ── La configuración ──────────────────────────────────────────────────────────
class TestLaConfiguracionRechazaLoIncoherente:
    def test_sin_identificadores_nadie_entra(self) -> None:
        with pytest.raises(ValueError, match="nadie podría iniciar sesión"):
            IdentityConfig(secret_key=TEST_SECRET_KEY, sign_in_identifiers=())

    def test_username_como_identificador_sin_politica(self) -> None:
        with pytest.raises(ValueError, match="ninguna política valida"):
            IdentityConfig(
                secret_key=TEST_SECRET_KEY, sign_in_identifiers=("username",)
            )

    def test_sin_mail_obligatorio_y_sin_usernames(self) -> None:
        with pytest.raises(ValueError, match="sin ningún identificador"):
            IdentityConfig(secret_key=TEST_SECRET_KEY, require_email=False)

    def test_el_default_sigue_siendo_solo_mail(self) -> None:
        """El comportamiento de 9.x no cambia para quien no configura nada."""
        config = IdentityConfig(secret_key=TEST_SECRET_KEY)
        assert config.usernames is None
        assert config.require_email is True
        assert config.sign_in_identifiers == ("email",)


# ── El alta ───────────────────────────────────────────────────────────────────
class TestElAlta:
    @pytest.mark.anyio
    async def test_se_puede_crear_una_cuenta_solo_con_username(self) -> None:
        contenedor = configure_test_identity(_config(require_email=False))
        servicio = contenedor.identity_service()

        usuario, codigo = await servicio.sign_up(
            username="indroic", password="una-frase-larga"
        )

        assert usuario.username == "indroic"
        assert usuario.email is None
        assert codigo is None, (
            "Sin mail no hay a dónde mandar el código, y devolver uno que nadie puede canjear "
            "sería peor que no devolver ninguno."
        )

    @pytest.mark.anyio
    async def test_el_username_se_guarda_normalizado(self) -> None:
        contenedor = configure_test_identity(_config(require_email=False))
        usuario, _ = await contenedor.identity_service().sign_up(
            username="  INDROIC  ", password="una-frase-larga"
        )
        assert usuario.username == "indroic"

    @pytest.mark.anyio
    async def test_un_username_tomado_da_409(self) -> None:
        contenedor = configure_test_identity(_config(require_email=False))
        servicio = contenedor.identity_service()

        await servicio.sign_up(username="indroic", password="una-frase-larga")
        with pytest.raises(UsernameAlreadyTakenError):
            await servicio.sign_up(username="INDROIC", password="otra-frase-larga")

    @pytest.mark.anyio
    async def test_sin_ningun_identificador_se_rechaza(self) -> None:
        """No depende de la config y no se puede desactivar: sería una fila inalcanzable."""
        contenedor = configure_test_identity(_config(require_email=False))
        with pytest.raises(ValueError, match="al menos un identificador"):
            await contenedor.identity_service().sign_up(password="una-frase-larga")

    @pytest.mark.anyio
    async def test_con_require_email_el_mail_sigue_siendo_obligatorio(self) -> None:
        contenedor = configure_test_identity(_config(require_email=True))
        with pytest.raises(ValueError, match="dirección de correo"):
            await contenedor.identity_service().sign_up(
                username="indroic", password="una-frase-larga"
            )

    @pytest.mark.anyio
    async def test_un_username_sin_politica_declarada_se_rechaza(self) -> None:
        """Aceptarlo guardaría un valor que ninguna política validó y ningún login puede usar."""
        contenedor = configure_test_identity(IdentityConfig(secret_key=TEST_SECRET_KEY))
        with pytest.raises(ValueError, match="no los tiene habilitados"):
            await contenedor.identity_service().sign_up(
                email="ana@ejemplo.com", username="ana", password="una-frase-larga"
            )


# ── El sign-in ────────────────────────────────────────────────────────────────
class TestElSignIn:
    @pytest.mark.anyio
    async def test_se_entra_con_el_username(self) -> None:
        contenedor = configure_test_identity(_config(require_email=False))
        servicio = contenedor.identity_service()

        await servicio.sign_up(username="indroic", password="una-frase-larga")
        usuario, _, par = await servicio.sign_in(
            identifier="INDROIC", password="una-frase-larga"
        )

        assert usuario.username == "indroic"
        assert par.access_token

    @pytest.mark.anyio
    async def test_se_entra_con_el_mail_cuando_los_dos_estan_habilitados(self) -> None:
        contenedor = configure_test_identity(_config())
        servicio = contenedor.identity_service()

        await servicio.sign_up(
            email="ana@ejemplo.com", username="ana", password="una-frase-larga"
        )
        usuario, _, _ = await servicio.sign_in(
            identifier="ana@ejemplo.com", password="una-frase-larga"
        )
        assert usuario.username == "ana"

    @pytest.mark.anyio
    async def test_una_cuenta_sin_mail_entra_con_require_verified_email(self) -> None:
        """
        El encierro que hay que evitar: sin mail nunca va a haber `email_verified=True`, así que
        exigirlo sería un 403 permanente y sin salida.
        """
        contenedor = configure_test_identity(
            _config(require_email=False, require_verified_email=True)
        )
        servicio = contenedor.identity_service()

        await servicio.sign_up(username="indroic", password="una-frase-larga")
        _, _, par = await servicio.sign_in(
            identifier="indroic", password="una-frase-larga"
        )
        assert par.access_token

    @pytest.mark.anyio
    async def test_una_cuenta_con_mail_sin_verificar_sigue_rechazada(self) -> None:
        """La política no se aflojó: sólo dejó de aplicarse donde no puede aplicarse."""
        contenedor = configure_test_identity(
            _config(require_verified_email=True)
        )
        servicio = contenedor.identity_service()

        await servicio.sign_up(email="ana@ejemplo.com", password="una-frase-larga")
        with pytest.raises(EmailNotVerifiedError):
            await servicio.sign_in(
                identifier="ana@ejemplo.com", password="una-frase-larga"
            )

    @pytest.mark.anyio
    async def test_no_se_entra_con_username_si_no_esta_habilitado(self) -> None:
        contenedor = configure_test_identity(
            _config(sign_in_identifiers=("email",))
        )
        servicio = contenedor.identity_service()

        await servicio.sign_up(
            email="ana@ejemplo.com", username="ana", password="una-frase-larga"
        )
        with pytest.raises(InvalidCredentialsError):
            await servicio.sign_in(identifier="ana", password="una-frase-larga")


# ── El invariante que sostiene todo ───────────────────────────────────────────
class TestElSenueloSeHasheaEnTodasLasRamas:
    """
    Cada rama del resolvedor de identificador es una oportunidad de retornar antes del señuelo.

    Si alguna lo hace, "no existe" responde en microsegundos y "contraseña equivocada" en
    decenas de milisegundos, y la diferencia enumera cuentas registradas sin adivinar ni una
    contraseña. Se cuenta la llamada a `hash` en vez de medir tiempos: medir tiempos en CI es
    escamoso, y lo que importa es que el trabajo se haga.
    """

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("identificador", "rama"),
        [
            ("nadie@ejemplo.com", "mail inexistente"),
            ("nadie", "username inexistente"),
            ("con espacios y @", "con arroba pero no es un mail válido"),
            ("MAYUSCULAS", "username inexistente, sin normalizar"),
        ],
    )
    async def test_un_identificador_inexistente_igual_hashea(
        self, identificador: str, rama: str
    ) -> None:
        hasher = HasherEspia()
        contenedor = configure_test_identity(
            _config(require_email=False), hasher=hasher
        )
        servicio = contenedor.identity_service()

        await servicio.sign_up(username="indroic", password="una-frase-larga")
        antes = hasher.hashes

        with pytest.raises(InvalidCredentialsError):
            await servicio.sign_in(identifier=identificador, password="lo-que-sea")

        assert hasher.hashes == antes + 1, (
            f"La rama '{rama}' retornó sin hashear el señuelo, así que responde mucho más "
            f"rápido que una contraseña equivocada. Eso enumera cuentas."
        )

    @pytest.mark.anyio
    async def test_una_forma_no_habilitada_tambien_hashea(self) -> None:
        """
        La rama más fácil de olvidar: el identificador tiene forma de username pero la app sólo
        acepta mails. Cortar ahí es gratis y es exactamente el oráculo.
        """
        hasher = HasherEspia()
        contenedor = configure_test_identity(
            _config(sign_in_identifiers=("email",)), hasher=hasher
        )
        servicio = contenedor.identity_service()

        await servicio.sign_up(
            email="ana@ejemplo.com", username="ana", password="una-frase-larga"
        )
        antes = hasher.hashes

        with pytest.raises(InvalidCredentialsError):
            await servicio.sign_in(identifier="ana", password="una-frase-larga")

        assert hasher.hashes == antes + 1


# ── Compatibilidad de la superficie ───────────────────────────────────────────
class TestLaCompatibilidadDeLaSuperficie:
    def test_el_cuerpo_http_acepta_los_tres_nombres(self) -> None:
        from hexcore.darwin.infrastructure.api.routers import SignInRequest

        assert (
            SignInRequest.model_validate(
                {"email": "ana@ejemplo.com", "password": "x"}
            ).credential
            == "ana@ejemplo.com"
        )
        assert (
            SignInRequest.model_validate({"username": "ana", "password": "x"}).credential
            == "ana"
        )
        assert (
            SignInRequest.model_validate({"identifier": "z", "password": "x"}).credential
            == "z"
        )

    def test_el_cuerpo_http_rechaza_varios_a_la_vez(self) -> None:
        from hexcore.darwin.infrastructure.api.routers import SignInRequest

        with pytest.raises(ValueError, match="varios identificadores"):
            SignInRequest.model_validate(
                {"email": "a@b.com", "username": "a", "password": "x"}
            )

    def test_el_cuerpo_http_dice_que_falta(self) -> None:
        from hexcore.darwin.infrastructure.api.routers import SignInRequest

        with pytest.raises(ValueError, match="Falta el identificador"):
            SignInRequest.model_validate({"password": "x"})

    def test_el_comando_acepta_los_tres_nombres(self) -> None:
        from hexcore.darwin.application.commands import SignIn

        assert SignIn(email="a@b.com", password="x").identifier == "a@b.com"
        assert SignIn(username="pepe", password="x").identifier == "pepe"
        assert SignIn(identifier="z", password="x").identifier == "z"

    @pytest.mark.anyio
    async def test_el_servicio_acepta_email_como_alias_deprecado(self) -> None:
        contenedor = configure_test_identity(_config())
        servicio = contenedor.identity_service()
        await servicio.sign_up(email="ana@ejemplo.com", password="una-frase-larga")

        with pytest.warns(DeprecationWarning, match="identifier"):
            usuario, _, _ = await servicio.sign_in(
                email="ana@ejemplo.com", password="una-frase-larga"
            )
        assert usuario.email == "ana@ejemplo.com"

    @pytest.mark.anyio
    async def test_pasar_los_dos_es_un_error(self) -> None:
        contenedor = configure_test_identity(_config())
        with pytest.raises(ValueError, match="nombre viejo del mismo parámetro"):
            await contenedor.identity_service().sign_in(
                identifier="a", email="b", password="x"
            )
