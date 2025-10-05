# Documentación HexCore: Estructura, Documentos, Repositorios y Abstracciones

## 1. Estructura de Directorios y Documentos

HexCore organiza el código siguiendo los principios DDD y arquitectura hexagonal. Los principales componentes son:

- **src/domain/**  
  Módulos de dominio, cada uno con entidades, repositorios, servicios, objetos de valor, eventos, enums y excepciones.
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

- **src/application/**  
  Casos de uso y DTOs para orquestar la lógica de negocio.

- **src/infrastructure/**  
  Implementaciones técnicas: ORM/ODM, CLI, caché, base de datos, repositorios, unit of work.

- **src/infrastructure/database/**
  - **models/**: Modelos SQLAlchemy (relacional).
  - **documents/**: Documentos Beanie (ODM para MongoDB).

- **tests/**  
  Pruebas para cada módulo de dominio e infraestructura.

---

## 2. Abstracciones de Entidades y Eventos

### BaseEntity

Clase base para entidades del dominio. Provee gestión de atributos comunes y manejo de eventos de dominio.

```python
from hexcore.domain.base import BaseEntity

class User(BaseEntity):
    id: UUID
    name: str
```

Las entidades heredan BaseEntity para activar el sistema de eventos y gestión de identidad.

---

### DomainEvent y eventos de entidad

Abstracciones para eventos del dominio y eventos de ciclo de vida de entidades.

```python
from hexcore.domain.events import DomainEvent, EntityCreatedEvent

class UserCreatedEvent(EntityCreatedEvent[User]):
    pass

user = User(...)
event = UserCreatedEvent(entity_id=user.id, payload={"name": user.name})
```

Estos eventos pueden ser disparados por entidades para notificar cambios significativos en el modelo de dominio.

---

## 3. Implementaciones de Repositorios: SQLAlchemy y Beanie ODM

### SQLAlchemyCommonImplementationsRepo

Repositorio genérico para modelos SQLAlchemy. Provee CRUD y requiere especificar la entidad, el modelo, la excepción "not found" y la unidad de trabajo.

```python name=hexcore/infrastructure/repositories/implementations.py
class SQLAlchemyCommonImplementationsRepo(BaseSQLAlchemyRepository[T], HasBasicArgs[T, M], t.Generic[T, M]):
    """
    Implementación común para repositorios SQL usando SQLAlchemy.
    Métodos principales:
      - get_by_id
      - list_all
      - save
      - delete
    """
    @property
    def model_cls(self) -> type[M]:
        raise NotImplementedError("Debes definir la clase modelo")

    async def get_by_id(self, entity_id: UUID) -> T:
        model = await sql_db_get(self.session, self.model_cls, entity_id, self.not_found_exception(entity_id))
        return await to_entity_from_model_or_document(model, self.entity_cls, self.fields_resolvers)

    async def list_all(self) -> t.List[T]:
        models = await sql_db_list(self.session, self.model_cls)
        return [await to_entity_from_model_or_document(model, self.entity_cls, self.fields_resolvers) for model in models]

    async def save(self, entity: T) -> T:
        saved = await sql_save_entity(self.session, entity, self.model_cls, fields_serializers=self.fields_serializers)
        return await to_entity_from_model_or_document(saved, self.entity_cls, self.fields_resolvers)

    async def delete(self, entity: T) -> None:
        await sql_logical_delete(self.session, entity, self.model_cls)
```

**Ejemplo de uso:**

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

---

### BeanieODMCommonImplementationsRepo

Repositorio genérico para documentos Beanie ODM (MongoDB). Provee CRUD y requiere la entidad, el documento, la excepción "not found", los resolvers y la unidad de trabajo.

```python name=hexcore/infrastructure/repositories/implementations.py
class BeanieODMCommonImplementationsRepo(IBaseRepository[T], HasBasicArgs[T, D], t.Generic[T, D]):
    """
    Implementación común para repositorios NoSQL usando Beanie ODM.
    Métodos principales:
      - get_by_id
      - list_all
      - save
      - delete
    """
    @property
    def document_cls(self) -> t.Type[D]:
        raise NotImplementedError("Debe implementar la propiedad document_cls")

    async def get_by_id(self, entity_id: UUID) -> T:
        document = await nosql_db_get(self.document_cls, entity_id)
        if not document:
            raise self.not_found_exception(entity_id)
        return await to_entity_from_model_or_document(document, self.entity_cls, self.fields_resolvers, is_nosql=True)

    async def list_all(self) -> t.List[T]:
        documents = await nosql_db_list(self.document_cls)
        return [await to_entity_from_model_or_document(doc, self.entity_cls, self.fields_resolvers, is_nosql=True) for doc in documents]

    @register_entity_on_uow
    async def save(self, entity: T) -> T:
        saved = await nosql_save_entity(entity, self.document_cls, self.fields_serializers)
        return await to_entity_from_model_or_document(saved, self.entity_cls, self.fields_resolvers, is_nosql=True)

    async def delete(self, entity: T) -> None:
        return await nosql_logical_delete(entity.id, self.document_cls)
```

**Ejemplo de uso:**

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

### Conversión entre modelos/documentos y entidades

Ambas implementaciones usan la función utilitaria `to_entity_from_model_or_document` para convertir instancias ORM/ODM en entidades de dominio, respetando la estructura de campos y aplicando resolvers para atributos complejos.

---

## 4. Inicialización y Descubrimiento de Documentos Beanie

Para inicializar y registrar todos los documentos Beanie automáticamente:

```python
from hexcore.infrastructure.repositories.orms.beanie.utils import init_beanie_documents

await init_beanie_documents()
```

Esto configura la conexión con MongoDB y registra todos los modelos Beanie implementados en el proyecto.

---

## 5. Referencias

- [CONTRIBUTING.md](./CONTRIBUTING.md): Pautas de colaboración.
- [README.md](./README.md): Introducción arquitectónica.
- [CHANGELOG.md](./CHANGELOG.md): Historial de cambios.

---

¿Quieres agregar ejemplos completos de definición de entidad, documento, evento y pruebas para tu dominio?
