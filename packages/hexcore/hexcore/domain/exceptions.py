class InactiveEntityException(Exception):
    """Excepción lanzada cuando se intenta operar con una entidad inactiva."""

    def __init__(self) -> None:
        super().__init__("La entidad no está activa.")
