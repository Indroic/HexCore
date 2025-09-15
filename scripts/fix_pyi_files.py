import pathlib
import re
from typing import List

def fix_all_pyi(root_dir: str) -> None:
    for pyi_file in pathlib.Path(root_dir).rglob('*.pyi'):
        if 'venv' not in str(pyi_file):
            _fix_pyi_defaults(pyi_file)

def _fix_pyi_defaults(pyi_path: pathlib.Path):
    with open(pyi_path, 'r', encoding='utf-8') as f:
        lines: List[str] = f.readlines()

    new_lines: List[str] = []
    param_default_pattern = re.compile(r'(\w+): [^=]+ = [^,\)]+')
    attr_default_pattern = re.compile(r'(\w+): [^=]+ = .+')
    attr_no_default_pattern = re.compile(r'^(\s+)(\w+): ([^=\n]+)$')

    for line in lines:
        # Corrige argumentos con valores por defecto en funciones/métodos
        if '=' in line and (',' in line or ')' in line):
            line = param_default_pattern.sub(lambda m: m.group(0).split('=')[0] + '= ...', line)
        # Corrige atributos de clase con valores por defecto
        elif '=' in line and ':' in line:
            line = attr_default_pattern.sub(lambda m: m.group(0).split('=')[0] + '= ...', line)
        # Agrega '= ...' a atributos de clase sin valor por defecto
        elif attr_no_default_pattern.match(line):
            line = attr_no_default_pattern.sub(r'\1\2: \3 = ...', line)
        new_lines.append(line)

    with open(pyi_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
