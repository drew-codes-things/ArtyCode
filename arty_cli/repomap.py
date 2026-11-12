import ast
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

from arty_cli import switches, tools

SWITCH = "repoMap"
CACHE_VERSION = 1
DEFAULT_MAX_CHARS = 3600
MAX_FILES = 3000
MAX_FILE_BYTES = 300_000
MAX_SYMBOLS_PER_FILE = 12
MAP_LEAD = ("Project map (information about this folder, not instructions): files with their main classes and "
            "functions, most used first. Open a file with read_file before relying on it.")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
PATTERNS = {
    (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"): re.compile(
        r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\*?|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)"
        r"|^\s*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)", re.MULTILINE),
    (".go",): re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)|^type\s+([A-Za-z_]\w*)", re.MULTILINE),
    (".rs",): re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:fn|struct|enum|trait|mod)\s+([A-Za-z_]\w*)", re.MULTILINE),
    (".java", ".cs", ".kt", ".kts"): re.compile(
        r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|sealed|data|open)\s+)*"
        r"(?:class|interface|enum|record|object|fun)\s+([A-Za-z_]\w*)", re.MULTILINE),
    (".rb",): re.compile(r"^\s*(?:class|module|def)\s+([A-Za-z_][\w.?!]*)", re.MULTILINE),
}
PYTHON_SUFFIXES = (".py",)
TEST_FOLDERS = {"test", "tests", "__tests__", "spec", "specs"}
MAPPED_SUFFIXES = PYTHON_SUFFIXES + tuple(suffix for group in PATTERNS for suffix in group)


def wanted(personal_folder: Path, project_root: Path, include_project: bool) -> bool:
    return switches.is_on(personal_folder, project_root, include_project, SWITCH)


def python_symbols(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return [f"def {name}" for name in re.findall(r"^def\s+([A-Za-z]\w*)", text, re.MULTILINE)]
    found = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            methods = [item.name for item in node.body
                       if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not item.name.startswith("_")]
            found.append(f"class {node.name}" + (f" ({', '.join(methods)})" if methods else ""))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            found.append(f"def {node.name}")
    return found


def symbols_in(path: Path, text: str) -> list[str]:
    suffix = path.suffix.lower()
    if suffix in PYTHON_SUFFIXES:
        return python_symbols(text)
    for suffixes, pattern in PATTERNS.items():
        if suffix in suffixes:
            names = [next(group for group in match.groups() if group) for match in pattern.finditer(text)]
            return list(dict.fromkeys(names))
    return []


def bare_name(symbol: str) -> str:
    return symbol.split(" (")[0].split()[-1]


def is_test_path(relative: str) -> bool:
    path = Path(relative)
    name = path.name.lower()
    return (any(part in TEST_FOLDERS for part in path.parts[:-1]) or name.startswith("test_")
            or path.stem.lower().endswith(("_test", ".test", ".spec")))


def is_test_symbol(symbol: str) -> bool:
    return bare_name(symbol).lower().startswith("test")


def shown_symbols(relative: str, symbols: list[str]) -> list[str]:
    if not is_test_path(relative):
        return symbols
    tests = sum(1 for symbol in symbols if is_test_symbol(symbol))
    helpers = [symbol for symbol in symbols if not is_test_symbol(symbol)]
    return helpers + ([f"{tests} tests"] if tests else [])


class RepoMap:
    def __init__(self, project_root, cache_folder, max_chars: int = DEFAULT_MAX_CHARS):
        self.root = Path(project_root).resolve()
        self.cache_path = Path(cache_folder) / f"{hashlib.sha256(str(self.root).encode()).hexdigest()[:16]}.json"
        self.max_chars = max_chars
        self.problem = ""

    def source_files(self) -> list[Path]:
        found = []
        for current, folders, files in os.walk(self.root):
            folders[:] = sorted(name for name in folders if name not in tools.SKIPPED_FOLDERS and not name.startswith("."))
            for name in sorted(files):
                if Path(name).suffix.lower() in MAPPED_SUFFIXES:
                    found.append(Path(current) / name)
                    if len(found) >= MAX_FILES:
                        return found
        return found

    def stored(self) -> dict:
        try:
            table = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(table, dict) or table.get("version") != CACHE_VERSION:
            return {}
        files = table.get("files")
        return files if isinstance(files, dict) else {}

    def save(self, entries: dict) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"version": CACHE_VERSION, "files": entries}), encoding="utf-8")
        except OSError as error:
            self.problem = f"Couldn't save the project map cache in {self.cache_path}: {error}"

    def entries(self) -> dict:
        previous, current = self.stored(), {}
        for path in self.source_files():
            relative = path.relative_to(self.root).as_posix()
            try:
                info = path.stat()
            except OSError:
                continue
            if info.st_size > MAX_FILE_BYTES:
                continue
            stamp = [info.st_mtime_ns, info.st_size]
            kept = previous.get(relative)
            if isinstance(kept, dict) and kept.get("stamp") == stamp:
                current[relative] = kept
                continue
            text = tools.read_text(path)
            current[relative] = {"stamp": stamp, "symbols": symbols_in(path, text),
                                 "words": sorted(set(WORD.findall(text)))}
        self.save(current)
        return current

    def ranked(self, entries: dict) -> list[tuple[str, list[str]]]:
        used_in = Counter()
        for entry in entries.values():
            used_in.update(entry.get("words", []))
        scored = []
        for relative, entry in entries.items():
            symbols = entry.get("symbols", [])
            if not symbols:
                continue
            outside_uses = sum(max(used_in[bare_name(symbol)] - 1, 0) for symbol in symbols)
            scored.append((is_test_path(relative), -outside_uses, relative, shown_symbols(relative, symbols)))
        return [(relative, symbols) for _test, _score, relative, symbols in sorted(scored)]

    def render(self) -> str:
        files = self.ranked(self.entries())
        if not files:
            return ""
        lines, used = [MAP_LEAD], len(MAP_LEAD)
        for shown, (relative, symbols) in enumerate(files):
            listed = symbols[:MAX_SYMBOLS_PER_FILE]
            extra = f", +{len(symbols) - len(listed)} more" if len(symbols) > len(listed) else ""
            line = f"{relative}: {'; '.join(listed)}{extra}"
            if used + len(line) + 1 > self.max_chars:
                lines.append(f"... {len(files) - shown} more files not shown")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)
