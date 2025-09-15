

import pathlib
def fix_types_pyi_aliases(pyi_path: pathlib.Path, py_path: pathlib.Path) -> None:
    with open(py_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    with open(pyi_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)