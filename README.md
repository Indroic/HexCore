# HexCore [![PyPI Downloads](https://static.pepy.tech/personalized-badge/hexcore?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/hexcore)
HexCore es un módulo base reutilizable para proyectos Python que implementan arquitectura hexagonal y event handling.

## ¿Qué provee HexCore?

- **Clases base y abstracciones** para entidades, repositorios, servicios y unidad de trabajo (UoW), siguiendo los principios de DDD y arquitectura hexagonal.
- **Interfaces y contratos** para caché, eventos y manejo de dependencias, desacoplando la lógica de negocio de la infraestructura.
- **Utilidades para event sourcing y event dispatching** listas para usar en cualquier proyecto.
- **Estructura flexible** para que puedas construir microservicios o aplicaciones monolíticas desacopladas y testeables.

---

## Instalación

```sh
pip install hexcore
```

---

## Pautas de Colaboración

¡Gracias por tu interés en contribuir a HexCore! Para mantener una colaboración organizada y eficiente, sigue estas pautas:

### 1. Código de Conducta
Mantén siempre una comunicación respetuosa y profesional. Revisa el [Código de Conducta](CODE_OF_CONDUCT.md) antes de interactuar.

### 2. Cómo Contribuir
- **Forkea** el repositorio y crea una rama para tu contribución (`feature/nombre`, `fix/nombre`, etc.).
- Realiza tus cambios en la rama y asegúrate de que el código funcione correctamente.
- Escribe una descripción clara y detallada en tu pull request (PR).
- Relaciona los issues relevantes en tu PR si aplica.

### 3. Estilo y Formato de Código
- Sigue la guía de estilos de Python ([PEP8](https://pep8.org/)).
- Usa comentarios cuando sea necesario para clarificar el propósito del código.
- Idealmente, incluye pruebas unitarias para nuevas funciones y arreglos.

### 4. Revisión de Pull Requests
- Todos los PR serán revisados antes de ser aceptados. Se pueden solicitar cambios o aclaraciones.
- Responde a los comentarios de los revisores para facilitar el proceso.

### 5. Issues
- Describe claramente los problemas que encuentres.
- Proporciona información relevante (logs, versiones, pasos para reproducir, etc.).

### 6. Comunicación
- Usa los issues y las discusiones para preguntas, sugerencias o propuestas.
- Si tienes dudas sobre cómo empezar, puedes abrir un issue para orientación.

### 7. Licencia
Al contribuir, aceptas que tu código será distribuido bajo la licencia del repositorio.

---

## Documentación Básica

### Estructura principal

HexCore se organiza con los siguientes submódulos y carpetas:

- **src/domain/**: Módulos de dominio, entidades, repositorios, servicios, objetos de valor, eventos, enums y excepciones.
  ```
  src/domain/{modulo}/
    ├─ __init__.py
    ├─ entities.py
    ├─ repositories.py
    ├─ services.py
    ├─ value_objects.py
    ├─ events.py
    ├─ enums.py
    └─ exceptions.py
  ```
- **src/application/**: Casos de uso (UseCase) y DTOs para orquestar la lógica de negocio.
- **src/infrastructure/**: Implementaciones técnicas (ORM/ODM, CLI, caché, base de datos, repositorios, unit of work).
- **src/infrastructure/database/models/**: Modelos SQLAlchemy para base de datos relacional.
- **src/infrastructure/database/documents/**: Documentos Beanie para MongoDB.
- **tests/**: Pruebas para módulos de dominio e infraestructura.

---

### Abstracciones de Entidades y Eventos

#### BaseEntity

Clase base para entidades de dominio. Provee atributos comunes y gestión de eventos.

```python
from hexcore.domain.base import BaseEntity

class User(BaseEntity):
    id: UUID
    name: str
```

#### DomainEvent y eventos de entidad

Abstracciones para eventos de dominio y para ciclo de vida de entidades.

```python
from hexcore.domain.events import DomainEvent, EntityCreatedEvent

class UserCreatedEvent(EntityCreatedEvent[User]):
    pass

user = User(...)
event = UserCreatedEvent(entity_id=user.id, payload={"name": user.name})
```

---

### Implementaciones de Repositorios

#### SQLAlchemyCommonImplementationsRepo

Repositorio genérico para modelos SQLAlchemy con métodos CRUD reutilizables.

```python
class SQLAlchemyCommonImplementationsRepo(BaseSQLAlchemyRepository[T], HasBasicArgs[T, M], t.Generic[T, M]):
    # Métodos principales: get_by_id, list_all, save, delete
    ...
```

**Ejemplo:**

```python
class UserRepository(SQLAlchemyCommonImplementationsRepo[UserEntity, UserModel]):
    def __init__(self, uow):
        super().__init__(
            entity_cls=UserEntity,
            model_cls=UserModel,
            not_found_exception=UserNotFoundException,
            fields_resolvers=None,
            fields_serializers=None,
            uow=uow
        )
```

#### BeanieODMCommonImplementationsRepo

Repositorio genérico para documentos Beanie ODM (MongoDB) con métodos CRUD reutilizables.

```python
class BeanieODMCommonImplementationsRepo(IBaseRepository[T], HasBasicArgs[T, D], t.Generic[T, D]):
    # Métodos principales: get_by_id, list_all, save, delete
    ...
```

**Ejemplo:**

```python
class UserRepository(BeanieODMCommonImplementationsRepo[UserEntity, UserDocument]):
    def __init__(self, uow):
        super().__init__(
            entity_cls=UserEntity,
            document_cls=UserDocument,
            not_found_exception=UserNotFoundException,
            fields_resolvers=None,
            fields_serializers=None,
            uow=uow
        )
```

---

### Inicialización y Descubrimiento de Documentos Beanie

Para inicializar y registrar automáticamente todos los documentos Beanie:

```python
from hexcore.infrastructure.repositories.orms.beanie.utils import init_beanie_documents

await init_beanie_documents()
```

---

### Conversión entre modelos/documentos y entidades

Ambos repositorios utilizan `to_entity_from_model_or_document` para convertir modelos ORM/ODM en entidades del dominio, aplicando resolvers para atributos complejos.

---

## Referencias

- [CONTRIBUTING.md](./CONTRIBUTING.md): Pautas de colaboración.
- [CHANGELOG.md](./CHANGELOG.md): Historial de cambios.
- [DOCS.md](./DOCS.md): Documentación básica de clases, funciones y ejemplos.
