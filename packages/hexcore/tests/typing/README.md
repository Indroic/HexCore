# Tests de tipo

Estos archivos **no se ejecutan**: se les pasa Pyright. `tests/test_typing_gate.py` lo
invoca sobre este directorio y exige cero errores.

Existen porque el repo no tenía ninguna verificación de que las fachadas tipen de verdad —
cero ocurrencias de `assert_type`, `reveal_type` o `pyright` bajo `tests/`. Sin esto, un
`.pyi` que se rompa o una regla de Pyright que se apague pasan inadvertidos.

No les pongas prefijo `test_`: pytest los colectaría e intentaría ejecutarlos.
