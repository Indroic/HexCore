"""
`drbac`: autorización contextual sobre `rbac` (Fase F4 del plan de rbac/drbac).

Esta fase sólo trae el lenguaje de condiciones (`conditions.py`) y el registro de predicados
con nombre (`predicates.py`) — dominio puro, sin persistencia y sin `DrbacPlugin` todavía. La
Fase F5 agrega `domain.py`, `pip.py`, `pdp.py`, `provider.py`, `cache.py`, `router.py` y el
plugin en sí, que depende de `rbac` (``DrbacPlugin.requires = ("rbac",)``).
"""
from __future__ import annotations
