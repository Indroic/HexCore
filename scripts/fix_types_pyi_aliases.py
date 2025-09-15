

import pathlib
import re
from typing import Dict

def _extract_type_aliases(py_path: pathlib.Path) -> Dict[str, str]:
    alias_map: dict[str, str] = {}
    pattern = re.compile(r'^(\w+): t.TypeAlias = (.+)$')
    with open(py_path, 'r', encoding='utf-8') as f:
        for line in f:
            m = pattern.match(line.strip())
            if m:
                alias_map[m.group(1)] = m.group(2)
    return alias_map

def fix_types_pyi_aliases(pyi_path: pathlib.Path, py_path: pathlib.Path) -> None:
    alias_map = _extract_type_aliases(py_path)
    with open(pyi_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines: list[str] = []
    for line in lines:
        m = re.match(r'^(\w+): t.TypeAlias\s*$', line.strip())
        if m and m.group(1) in alias_map:
            alias = m.group(1)
            new_line = f"{alias}: t.TypeAlias = {alias_map[alias]}\n"
            new_lines.append(new_line)
        else:
            new_lines.append(line)

    with open(pyi_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)