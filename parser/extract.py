"""
parser/extract.py — AST extraction engine.

Based on graphify's LanguageConfig architecture (github.com/safishamsi/graphify),
with the following upgrades:
  - Node IDs: "{repo_id}::{rel_path}::{ClassName}::{method}" (never bare method names)
  - Node labels: "ClassName::methodName" (not ".methodName()")
  - Paths: repo-relative, not absolute
  - file_hash field on every node (for incremental indexing)
  - No external dependencies — pure tree-sitter + stdlib
"""
from __future__ import annotations

import hashlib
import importlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# ── ID helpers ────────────────────────────────────────────────────────────────

def make_node_id(repo_id: str, rel_path: str, class_name: str | None, method_name: str | None) -> str:
    """
    Build canonical node ID.

    Examples:
      file:    make_node_id("myrepo", "app/Services/Order.php", None, None)
               → "myrepo::app/Services/Order.php"
      class:   make_node_id("myrepo", "app/Services/Order.php", "OrderService", None)
               → "myrepo::app/Services/Order.php::OrderService"
      method:  make_node_id("myrepo", "app/Services/Order.php", "OrderService", "__construct")
               → "myrepo::app/Services/Order.php::OrderService::__construct"
    """
    parts = [repo_id, rel_path]
    if class_name:
        parts.append(class_name)
    if method_name:
        parts.append(method_name)
    return "::".join(parts)


def make_label(class_name: str | None, method_name: str | None, entity_name: str) -> str:
    """
    Human-readable label. Always "ClassName::method", never bare "method".

    Examples:
      class:  make_label("OrderService", None, "OrderService") → "OrderService"
      method: make_label("OrderService", "__construct", "__construct") → "OrderService::__construct"
      func:   make_label(None, None, "helperFn") → "helperFn"
    """
    if class_name and method_name:
        return f"{class_name}::{method_name}"
    return entity_name


def file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


# ── LanguageConfig ────────────────────────────────────────────────────────────

@dataclass
class LanguageConfig:
    ts_module: str
    ts_language_fn: str = "language"

    class_types: frozenset = frozenset()
    function_types: frozenset = frozenset()
    import_types: frozenset = frozenset()
    call_types: frozenset = frozenset()
    static_prop_types: frozenset = frozenset()
    helper_fn_names: frozenset = frozenset()
    container_bind_methods: frozenset = frozenset()
    event_listener_properties: frozenset = frozenset()

    name_field: str = "name"
    name_fallback_child_types: tuple = ()
    body_field: str = "body"
    body_fallback_child_types: tuple = ()

    decorator_types: frozenset = frozenset()         # e.g. {"decorator"} for TS/Python, {"marker_annotation"} for Java
    decorator_wrapper_types: frozenset = frozenset() # nodes that wrap decorator+class, e.g. {"export_statement"} for TS

    call_function_field: str = "function"
    call_accessor_node_types: frozenset = frozenset()
    call_accessor_field: str = "attribute"
    function_boundary_types: frozenset = frozenset()

    import_handler: Callable | None = None
    resolve_function_name_fn: Callable | None = None
    extra_walk_fn: Callable | None = None


# ── Text helpers ──────────────────────────────────────────────────────────────

def _read_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _extract_decorator_name(node, source: bytes) -> str | None:
    """
    Extract a human-readable name from a decorator / annotation node.

    Handles:
      - Python/TS/JS:  @Injectable, @Injectable(), @router.get("/path")
      - Java:          @Service, @Autowired  (marker_annotation / annotation)
      - C#:            [HttpGet], [Authorize] (attribute node)
    """
    raw = _read_text(node, source).strip()

    # Python / TS / JS: text starts with '@'
    if raw.startswith("@"):
        name = raw.split("(")[0].split("\n")[0].strip()
        return name if len(name) > 1 else None

    # Java marker_annotation / C# attribute: look for identifier child
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "qualified_name",
                           "scoped_identifier", "name", "qualified_identifier"):
            return f"@{_read_text(child, source)}"

    return None


def _find_body(node, config: LanguageConfig):
    b = node.child_by_field_name(config.body_field)
    if b:
        return b
    for child in node.children:
        if child.type in config.body_fallback_child_types:
            return child
    return None


# ── Import handlers (from graphify, unchanged) ────────────────────────────────

def _import_python(node, source, file_nid, stem, edges, str_path):
    t = node.type
    if t == "import_statement":
        for child in node.children:
            if child.type in ("dotted_name", "aliased_import"):
                raw = _read_text(child, source)
                module_name = raw.split(" as ")[0].strip().lstrip(".")
                edges.append({"_src": file_nid, "_tgt_name": module_name,
                               "relation": "imports", "confidence": "EXTRACTED",
                               "source_file": str_path, "line": node.start_point[0] + 1})
    elif t == "import_from_statement":
        module_node = node.child_by_field_name("module_name")
        if module_node:
            raw = _read_text(module_node, source)
            module_name = raw.lstrip(".")
            edges.append({"_src": file_nid, "_tgt_name": module_name,
                           "relation": "imports_from", "confidence": "EXTRACTED",
                           "source_file": str_path, "line": node.start_point[0] + 1})


def _import_js(node, source, file_nid, stem, edges, str_path):
    for child in node.children:
        if child.type == "string":
            raw = _read_text(child, source).strip("'\"` ")
            if raw:
                module_name = raw.split("/")[-1]
                edges.append({"_src": file_nid, "_tgt_name": module_name,
                               "relation": "imports_from", "confidence": "EXTRACTED",
                               "source_file": str_path, "line": node.start_point[0] + 1})
            break


def _import_java(node, source, file_nid, stem, edges, str_path):
    for child in node.children:
        if child.type in ("scoped_identifier", "identifier"):
            raw = _read_text(child, source)
            module_name = raw.split(".")[-1].strip("*").strip(".")
            if module_name:
                edges.append({"_src": file_nid, "_tgt_name": module_name,
                               "relation": "imports", "confidence": "EXTRACTED",
                               "source_file": str_path, "line": node.start_point[0] + 1})
            break


def _import_c(node, source, file_nid, stem, edges, str_path):
    for child in node.children:
        if child.type in ("string_literal", "system_lib_string", "string"):
            raw = _read_text(child, source).strip('"<> ')
            module_name = raw.split("/")[-1].split(".")[0]
            if module_name:
                edges.append({"_src": file_nid, "_tgt_name": module_name,
                               "relation": "imports", "confidence": "EXTRACTED",
                               "source_file": str_path, "line": node.start_point[0] + 1})
            break


def _import_csharp(node, source, file_nid, stem, edges, str_path):
    for child in node.children:
        if child.type in ("qualified_name", "identifier", "name_equals"):
            raw = _read_text(child, source)
            module_name = raw.split(".")[-1].strip()
            if module_name:
                edges.append({"_src": file_nid, "_tgt_name": module_name,
                               "relation": "imports", "confidence": "EXTRACTED",
                               "source_file": str_path, "line": node.start_point[0] + 1})
            break


def _import_kotlin(node, source, file_nid, stem, edges, str_path):
    path_node = node.child_by_field_name("path")
    if path_node:
        raw = _read_text(path_node, source)
        module_name = raw.split(".")[-1].strip()
        if module_name:
            edges.append({"_src": file_nid, "_tgt_name": module_name,
                           "relation": "imports", "confidence": "EXTRACTED",
                           "source_file": str_path, "line": node.start_point[0] + 1})
        return
    for child in node.children:
        if child.type == "identifier":
            raw = _read_text(child, source)
            edges.append({"_src": file_nid, "_tgt_name": raw,
                           "relation": "imports", "confidence": "EXTRACTED",
                           "source_file": str_path, "line": node.start_point[0] + 1})
            break


def _import_scala(node, source, file_nid, stem, edges, str_path):
    for child in node.children:
        if child.type in ("stable_id", "identifier"):
            raw = _read_text(child, source)
            module_name = raw.split(".")[-1].strip("{} ")
            if module_name and module_name != "_":
                edges.append({"_src": file_nid, "_tgt_name": module_name,
                               "relation": "imports", "confidence": "EXTRACTED",
                               "source_file": str_path, "line": node.start_point[0] + 1})
            break


def _import_php(node, source, file_nid, stem, edges, str_path):
    for child in node.children:
        if child.type in ("qualified_name", "name", "identifier"):
            raw = _read_text(child, source)
            module_name = raw.split("\\")[-1].strip()
            if module_name:
                edges.append({"_src": file_nid, "_tgt_name": module_name,
                               "relation": "imports", "confidence": "EXTRACTED",
                               "source_file": str_path, "line": node.start_point[0] + 1})
            break


def _import_swift(node, source, file_nid, stem, edges, str_path):
    for child in node.children:
        if child.type == "identifier":
            raw = _read_text(child, source)
            edges.append({"_src": file_nid, "_tgt_name": raw,
                           "relation": "imports", "confidence": "EXTRACTED",
                           "source_file": str_path, "line": node.start_point[0] + 1})
            break


# ── C/C++ name helpers ────────────────────────────────────────────────────────

def _get_c_func_name(node, source):
    if node.type == "identifier":
        return _read_text(node, source)
    decl = node.child_by_field_name("declarator")
    if decl:
        return _get_c_func_name(decl, source)
    for child in node.children:
        if child.type == "identifier":
            return _read_text(child, source)
    return None


def _get_cpp_func_name(node, source):
    if node.type == "identifier":
        return _read_text(node, source)
    if node.type == "qualified_identifier":
        name_node = node.child_by_field_name("name")
        if name_node:
            return _read_text(name_node, source)
    decl = node.child_by_field_name("declarator")
    if decl:
        return _get_cpp_func_name(decl, source)
    for child in node.children:
        if child.type == "identifier":
            return _read_text(child, source)
    return None


# ── Language configs ──────────────────────────────────────────────────────────

_PYTHON_CONFIG = LanguageConfig(
    ts_module="tree_sitter_python",
    class_types=frozenset({"class_definition"}),
    function_types=frozenset({"function_definition", "decorated_definition"}),
    import_types=frozenset({"import_statement", "import_from_statement"}),
    call_types=frozenset({"call"}),
    decorator_types=frozenset({"decorator"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"attribute"}),
    call_accessor_field="attribute",
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_python,
)

_JS_CONFIG = LanguageConfig(
    ts_module="tree_sitter_javascript",
    class_types=frozenset({"class_declaration"}),
    function_types=frozenset({"function_declaration", "method_definition"}),
    import_types=frozenset({"import_statement"}),
    call_types=frozenset({"call_expression"}),
    decorator_types=frozenset({"decorator"}),
    decorator_wrapper_types=frozenset({"export_statement"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_expression"}),
    call_accessor_field="property",
    function_boundary_types=frozenset({"function_declaration", "arrow_function", "method_definition"}),
    import_handler=_import_js,
)

_TS_CONFIG = LanguageConfig(
    ts_module="tree_sitter_typescript",
    ts_language_fn="language_typescript",
    class_types=frozenset({"class_declaration"}),
    function_types=frozenset({"function_declaration", "method_definition"}),
    import_types=frozenset({"import_statement"}),
    call_types=frozenset({"call_expression"}),
    decorator_types=frozenset({"decorator"}),
    decorator_wrapper_types=frozenset({"export_statement"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_expression"}),
    call_accessor_field="property",
    function_boundary_types=frozenset({"function_declaration", "arrow_function", "method_definition"}),
    import_handler=_import_js,
)

_GO_CONFIG = LanguageConfig(
    ts_module="tree_sitter_go",
    class_types=frozenset({"type_declaration"}),
    function_types=frozenset({"function_declaration", "method_declaration"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"selector_expression"}),
    call_accessor_field="field",
    name_fallback_child_types=("type_identifier",),
    body_fallback_child_types=("block",),
    function_boundary_types=frozenset({"function_declaration", "method_declaration"}),
)

_JAVA_CONFIG = LanguageConfig(
    ts_module="tree_sitter_java",
    class_types=frozenset({"class_declaration", "interface_declaration"}),
    function_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"method_invocation"}),
    decorator_types=frozenset({"marker_annotation", "annotation"}),
    call_function_field="name",
    call_accessor_node_types=frozenset(),
    function_boundary_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_handler=_import_java,
)

_RUST_CONFIG = LanguageConfig(
    ts_module="tree_sitter_rust",
    class_types=frozenset({"impl_item", "struct_item", "trait_item"}),
    function_types=frozenset({"function_item"}),
    import_types=frozenset({"use_declaration"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"field_expression", "scoped_identifier"}),
    call_accessor_field="field",
    function_boundary_types=frozenset({"function_item"}),
)

_PHP_CONFIG = LanguageConfig(
    ts_module="tree_sitter_php",
    ts_language_fn="language_php",
    class_types=frozenset({"class_declaration"}),
    function_types=frozenset({"function_definition", "method_declaration"}),
    import_types=frozenset({"namespace_use_clause"}),
    call_types=frozenset({"function_call_expression", "member_call_expression",
                          "scoped_call_expression", "class_constant_access_expression"}),
    static_prop_types=frozenset({"scoped_property_access_expression"}),
    helper_fn_names=frozenset({"config"}),
    container_bind_methods=frozenset({"bind", "singleton", "scoped", "instance"}),
    event_listener_properties=frozenset({"listen", "subscribe"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_call_expression"}),
    call_accessor_field="name",
    name_fallback_child_types=("name",),
    body_fallback_child_types=("declaration_list", "compound_statement"),
    function_boundary_types=frozenset({"function_definition", "method_declaration"}),
    import_handler=_import_php,
)

_CSHARP_CONFIG = LanguageConfig(
    ts_module="tree_sitter_c_sharp",
    class_types=frozenset({"class_declaration", "interface_declaration"}),
    function_types=frozenset({"method_declaration"}),
    import_types=frozenset({"using_directive"}),
    call_types=frozenset({"invocation_expression"}),
    decorator_types=frozenset({"attribute"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_access_expression"}),
    call_accessor_field="name",
    body_fallback_child_types=("declaration_list",),
    function_boundary_types=frozenset({"method_declaration"}),
    import_handler=_import_csharp,
)

_KOTLIN_CONFIG = LanguageConfig(
    ts_module="tree_sitter_kotlin",
    class_types=frozenset({"class_declaration", "object_declaration"}),
    function_types=frozenset({"function_declaration"}),
    import_types=frozenset({"import_header"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="",
    call_accessor_node_types=frozenset({"navigation_expression"}),
    call_accessor_field="",
    name_fallback_child_types=("simple_identifier",),
    body_fallback_child_types=("function_body", "class_body"),
    function_boundary_types=frozenset({"function_declaration"}),
    import_handler=_import_kotlin,
)

_SCALA_CONFIG = LanguageConfig(
    ts_module="tree_sitter_scala",
    class_types=frozenset({"class_definition", "object_definition"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="",
    call_accessor_node_types=frozenset({"field_expression"}),
    call_accessor_field="field",
    name_fallback_child_types=("identifier",),
    body_fallback_child_types=("template_body",),
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_scala,
)

_RUBY_CONFIG = LanguageConfig(
    ts_module="tree_sitter_ruby",
    class_types=frozenset({"class"}),
    function_types=frozenset({"method", "singleton_method"}),
    import_types=frozenset(),
    call_types=frozenset({"call"}),
    call_function_field="method",
    call_accessor_node_types=frozenset(),
    name_fallback_child_types=("constant", "scope_resolution", "identifier"),
    body_fallback_child_types=("body_statement",),
    function_boundary_types=frozenset({"method", "singleton_method"}),
)


# ── Extension → config map ────────────────────────────────────────────────────

EXTENSION_CONFIG: dict[str, LanguageConfig] = {
    ".py":    _PYTHON_CONFIG,
    ".js":    _JS_CONFIG,
    ".jsx":   _JS_CONFIG,
    ".ts":    _TS_CONFIG,
    ".tsx":   _TS_CONFIG,
    ".go":    _GO_CONFIG,
    ".java":  _JAVA_CONFIG,
    ".rs":    _RUST_CONFIG,
    ".php":   _PHP_CONFIG,
    ".cs":    _CSHARP_CONFIG,
    ".kt":    _KOTLIN_CONFIG,
    ".kts":   _KOTLIN_CONFIG,
    ".scala": _SCALA_CONFIG,
    ".rb":    _RUBY_CONFIG,
}

SUPPORTED_EXTENSIONS = set(EXTENSION_CONFIG.keys())


# ── Generic extractor (our version) ──────────────────────────────────────────

def extract_file(
    path: Path,
    repo_id: str,
    repo_root: Path,
) -> dict:
    """
    Extract nodes and edges from a single source file.

    Returns:
        {
            "nodes": list of node dicts,
            "edges": list of edge dicts (may have _tgt_name for cross-file resolution),
            "file_hash": str,
            "error": str | None,
        }
    """
    ext = path.suffix.lower()
    config = EXTENSION_CONFIG.get(ext)
    if config is None:
        return {"nodes": [], "edges": [], "file_hash": None, "error": f"Unsupported extension: {ext}"}

    # Load tree-sitter language
    try:
        mod = importlib.import_module(config.ts_module)
        from tree_sitter import Language, Parser
        lang_fn = getattr(mod, config.ts_language_fn, None) or getattr(mod, "language", None)
        if lang_fn is None:
            return {"nodes": [], "edges": [], "file_hash": None, "error": f"No language fn in {config.ts_module}"}
        language = Language(lang_fn())
    except ImportError:
        return {"nodes": [], "edges": [], "file_hash": None, "error": f"{config.ts_module} not installed"}
    except Exception as e:
        return {"nodes": [], "edges": [], "file_hash": None, "error": str(e)}

    try:
        fhash = file_hash(path)
        source = path.read_bytes()
        parser = Parser(language)
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "file_hash": None, "error": str(e)}

    try:
        rel_path = str(path.relative_to(repo_root))
    except ValueError:
        rel_path = str(path)

    str_path = rel_path
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    function_bodies: list[tuple[str, str | None, object]] = []  # (func_nid, class_name, body_node)

    # File node
    file_nid = make_node_id(repo_id, rel_path, None, None)
    nodes.append({
        "id": file_nid,
        "repo_id": repo_id,
        "type": "file",
        "name": path.name,
        "file_path": rel_path,
        "language": _lang_name(config),
        "line_start": 1,
        "line_end": source.count(b"\n") + 1,
        "docstring": None,
        "file_hash": fhash,
    })
    seen_ids.add(file_nid)

    def add_node(nid: str, node_type: str, name: str, language: str,
                 line_start: int, line_end: int, docstring: str | None = None,
                 metadata: dict | None = None) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "repo_id": repo_id,
                "type": node_type,
                "name": name,
                "file_path": rel_path,
                "language": language,
                "line_start": line_start,
                "line_end": line_end,
                "docstring": docstring,
                "metadata": metadata or {},
                "file_hash": fhash,
            })

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0,
                 tgt_name: str | None = None, metadata: dict | None = None) -> None:
        edges.append({
            "_src": src,
            "_tgt": tgt if not tgt_name else None,
            "_tgt_name": tgt_name,
            "relation": relation,
            "confidence": confidence,
            "weight": weight,
            "source_file": str_path,
            "line": line,
            "metadata": metadata or {},
        })

    lang = _lang_name(config)

    def walk(node, parent_class_nid: str | None = None, parent_class_name: str | None = None,
             _decorators: list | None = None) -> None:
        t = node.type

        # ── TS/JS: export_statement (and similar wrappers) may contain decorators ─
        if config.decorator_wrapper_types and t in config.decorator_wrapper_types:
            decs: list[str] = []
            inner = None
            for child in node.children:
                if child.type in config.decorator_types:
                    name = _extract_decorator_name(child, source)
                    if name:
                        decs.append(name)
                elif child.type in config.class_types or child.type in config.function_types:
                    inner = child
            if inner:
                walk(inner, parent_class_nid, parent_class_name, _decorators=decs or None)
            else:
                # e.g. export_statement with no class/function (just export default, etc.)
                for child in node.children:
                    if child.type not in config.decorator_types:
                        walk(child, parent_class_nid, parent_class_name)
            return

        # ── Python: decorated_definition wraps decorators + class/function ────
        if t == "decorated_definition":
            decs: list[str] = []
            inner = None
            for child in node.children:
                if child.type == "decorator":
                    name = _extract_decorator_name(child, source)
                    if name:
                        decs.append(name)
                elif (child.type in config.class_types or
                      child.type in config.function_types or
                      child.type in ("async_function_definition",)):
                    inner = child
            if inner:
                walk(inner, parent_class_nid, parent_class_name, _decorators=decs or None)
            return

        # ── TS/JS/Java/C#: decorators are direct children of class/method ─────
        if not _decorators and config.decorator_types:
            decs = []
            for child in node.children:
                if child.type in config.decorator_types:
                    name = _extract_decorator_name(child, source)
                    if name:
                        decs.append(name)
                # Java: annotations live inside a modifiers node
                elif child.type == "modifiers":
                    for mod in child.children:
                        if mod.type in config.decorator_types:
                            name = _extract_decorator_name(mod, source)
                            if name:
                                decs.append(name)
                # C#: attributes live inside attribute_list
                elif child.type == "attribute_list":
                    for attr in child.children:
                        if attr.type in config.decorator_types:
                            name = _extract_decorator_name(attr, source)
                            if name:
                                decs.append(name)
            _decorators = decs or None

        # Imports
        if t in config.import_types:
            if config.import_handler:
                config.import_handler(node, source, file_nid, path.stem, edges, str_path)
            return

        # Classes
        if t in config.class_types:
            name_node = node.child_by_field_name(config.name_field)
            if name_node is None:
                for child in node.children:
                    if child.type in config.name_fallback_child_types:
                        name_node = child
                        break
            if not name_node:
                for child in node.children:
                    walk(child, parent_class_nid, parent_class_name)
                return

            class_name = _read_text(name_node, source)
            class_nid = make_node_id(repo_id, rel_path, class_name, None)
            line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1

            meta = {"decorators": _decorators} if _decorators else {}
            add_node(class_nid, "class", class_name, lang, line, end_line, metadata=meta)
            add_edge(file_nid, class_nid, "contains", line)

            # Inheritance (PHP, Python, C#, Swift)
            _extract_inherits(node, source, config, class_nid, repo_id, rel_path, add_edge, line)

            body = _find_body(node, config)
            if body:
                pending_decs: list[str] = []
                for child in body.children:
                    if config.decorator_types and child.type in config.decorator_types:
                        # Accumulate method-level decorator for the next function node
                        name = _extract_decorator_name(child, source)
                        if name:
                            pending_decs.append(name)
                    elif child.type in config.function_types or child.type in config.class_types:
                        walk(child, parent_class_nid=class_nid, parent_class_name=class_name,
                             _decorators=pending_decs or None)
                        pending_decs = []
                    else:
                        pending_decs = []
                        walk(child, parent_class_nid=class_nid, parent_class_name=class_name)
            return

        # Functions / methods
        if t in config.function_types:
            func_name = _resolve_func_name(node, source, config, t)
            if not func_name:
                return

            line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1

            meta = {"decorators": _decorators} if _decorators else {}
            if parent_class_nid and parent_class_name:
                func_nid = make_node_id(repo_id, rel_path, parent_class_name, func_name)
                label = make_label(parent_class_name, func_name, func_name)
                add_node(func_nid, "function", label, lang, line, end_line, metadata=meta)
                add_edge(parent_class_nid, func_nid, "method", line)
            else:
                func_nid = make_node_id(repo_id, rel_path, None, func_name)
                label = make_label(None, None, func_name)
                add_node(func_nid, "function", label, lang, line, end_line, metadata=meta)
                add_edge(file_nid, func_nid, "contains", line)

            body = _find_body(node, config)
            if body:
                function_bodies.append((func_nid, parent_class_name, body))
            return

        # Default: recurse
        for child in node.children:
            walk(child, parent_class_nid, parent_class_name)

    walk(root)

    # ── Call-graph pass ───────────────────────────────────────────────────────
    # Build label → nid lookup (for same-file call resolution)
    name_to_nid: dict[str, str] = {}
    for n in nodes:
        name_to_nid[n["name"].lower()] = n["id"]
        # Also index by short method name for within-file resolution
        short = n["name"].split("::")[-1].lower()
        if short not in name_to_nid:
            name_to_nid[short] = n["id"]

    seen_call_pairs: set[tuple[str, str]] = set()

    for func_nid, class_name, body in function_bodies:
        _walk_calls(body, source, config, func_nid, name_to_nid, edges, seen_call_pairs, str_path)

    return {"nodes": nodes, "edges": edges, "file_hash": fhash, "error": None}


# ── Call walking ──────────────────────────────────────────────────────────────

def _walk_calls(node, source, config, caller_nid, name_to_nid, edges, seen_pairs, str_path):
    if node.type in config.function_boundary_types:
        return
    if node.type in config.call_types:
        callee = _resolve_callee(node, source, config)
        if callee:
            tgt_nid = name_to_nid.get(callee.lower())
            line = node.start_point[0] + 1
            if tgt_nid and tgt_nid != caller_nid:
                pair = (caller_nid, tgt_nid)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    edges.append({"_src": caller_nid, "_tgt": tgt_nid, "_tgt_name": None,
                                   "relation": "calls", "confidence": "EXTRACTED",
                                   "weight": 1.0, "source_file": str_path, "line": line, "metadata": {}})
            elif callee:
                # Cross-file call — save for resolution in indexer
                edges.append({"_src": caller_nid, "_tgt": None, "_tgt_name": callee,
                               "relation": "calls", "confidence": "INFERRED",
                               "weight": 0.7, "source_file": str_path, "line": line, "metadata": {}})
    for child in node.children:
        _walk_calls(child, source, config, caller_nid, name_to_nid, edges, seen_pairs, str_path)


def _resolve_callee(node, source, config) -> str | None:
    """Extract the callee name from a call node."""
    if config.ts_module == "tree_sitter_php":
        if node.type == "function_call_expression":
            fn = node.child_by_field_name("function")
            return _read_text(fn, source) if fn else None
        elif node.type == "scoped_call_expression":
            scope = node.child_by_field_name("scope")
            return _read_text(scope, source) if scope else None
        else:
            name = node.child_by_field_name("name")
            return _read_text(name, source) if name else None
    elif config.call_function_field:
        fn = node.child_by_field_name(config.call_function_field)
        if fn:
            if fn.type == "identifier":
                return _read_text(fn, source)
            elif fn.type in config.call_accessor_node_types:
                if config.call_accessor_field:
                    attr = fn.child_by_field_name(config.call_accessor_field)
                    return _read_text(attr, source) if attr else None
            else:
                return _read_text(fn, source)
    return None


# ── Inheritance extraction ────────────────────────────────────────────────────

def _extract_inherits(node, source, config, class_nid, repo_id, rel_path, add_edge, line):
    """Extract extends/implements edges for PHP, Python, C#."""
    if config.ts_module == "tree_sitter_php":
        for child in node.children:
            if child.type == "base_clause":
                parent_name_node = child.children[-1] if child.children else None
                if parent_name_node:
                    parent_name = _read_text(parent_name_node, source)
                    add_edge(class_nid, None, "inherits", line,
                             tgt_name=parent_name, confidence="EXTRACTED")
            elif child.type == "class_implements":
                for iface in child.named_children:
                    iface_name = _read_text(iface, source)
                    if iface_name:
                        add_edge(class_nid, None, "implements", line,
                                 tgt_name=iface_name, confidence="EXTRACTED")
    elif config.ts_module == "tree_sitter_python":
        args = node.child_by_field_name("superclasses")
        if args:
            for arg in args.children:
                if arg.type == "identifier":
                    base = _read_text(arg, source)
                    add_edge(class_nid, None, "inherits", line,
                             tgt_name=base, confidence="EXTRACTED")


# ── Helper functions ──────────────────────────────────────────────────────────

def _resolve_func_name(node, source, config, node_type) -> str | None:
    if config.resolve_function_name_fn is not None:
        declarator = node.child_by_field_name("declarator")
        return config.resolve_function_name_fn(declarator, source) if declarator else None
    name_node = node.child_by_field_name(config.name_field)
    if name_node is None:
        for child in node.children:
            if child.type in config.name_fallback_child_types:
                name_node = child
                break
    return _read_text(name_node, source) if name_node else None


def _lang_name(config: LanguageConfig) -> str:
    return config.ts_module.replace("tree_sitter_", "").replace("_", "")


def supported_extensions() -> set[str]:
    return SUPPORTED_EXTENSIONS
