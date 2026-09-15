"""
Submódulo de casos de uso base para la aplicación.
"""

from .base import UseCase
from .query import ListEntitiesUseCase, QueryEntitiesUseCase, SearchEntitiesUseCase

__all__ = [
	"UseCase",
	"QueryEntitiesUseCase",
	"ListEntitiesUseCase",
	"SearchEntitiesUseCase",
]
