"""AST-based scanner for environment variable usages in Python code.

The scanner understands stdlib ``os`` lookups, django-environ and
python-decouple. Imports and common aliases are resolved without importing or
executing the inspected project.
"""

from __future__ import annotations

import ast
import copy
import os
import stat
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple


@dataclass
class EnvUsage:
    """A single occurrence of an env var lookup in source code."""

    name: Optional[str]
    """Variable name if it could be resolved statically, else None."""

    file: Path
    """File where the usage was found."""

    line: int
    """1-based line number."""

    has_default: bool = False
    """True if a default value was provided."""

    call_type: str = "getenv"
    """How the environment variable was looked up."""

    raw_expr: Optional[str] = None
    """Text of an unresolved name expression, for diagnostics."""

    default_node: Optional[ast.AST] = None
    """AST node for an explicitly supplied or declared default value."""

    @property
    def is_dynamic(self) -> bool:
        return self.name is None


@dataclass
class ScanResult:
    """Aggregated result of scanning one or more files."""

    usages: List[EnvUsage] = field(default_factory=list)
    scanned_files: List[Path] = field(default_factory=list)
    errors: List[Tuple[Path, str]] = field(default_factory=list)

    @property
    def static_names(self) -> Set[str]:
        """Unique set of statically-resolved variable names."""
        return {usage.name for usage in self.usages if usage.name is not None}

    @property
    def dynamic_usages(self) -> List[EnvUsage]:
        return [usage for usage in self.usages if usage.is_dynamic]

    @property
    def names_with_defaults(self) -> Set[str]:
        """Names that have at least one usage with a default value."""
        return {
            usage.name
            for usage in self.usages
            if usage.name is not None and usage.has_default
        }


class ScanError(Exception):
    """Raised when a file or project tree cannot be scanned safely."""


# Zero-based positional index of `default` in django-environ's methods.
# Env.__call__, list/tuple/dict and get_value have `cast` before it.
_DJANGO_DEFAULT_POSITIONS = {
    "str": 1,
    "bytes": 1,
    "bool": 1,
    "int": 1,
    "float": 1,
    "json": 1,
    "list": 2,
    "tuple": 2,
    "dict": 2,
    "url": 1,
    "db_url": 1,
    "db": 1,
    "cache_url": 1,
    "cache": 1,
    "email_url": 1,
    "email": 1,
    "search_url": 1,
    "channels_url": 1,
    "channels": 1,
    "path": 1,
    "get_value": 2,
}

_DJANGO_IMPLICIT_NAMES = {
    "db_url": "DATABASE_URL",
    "db": "DATABASE_URL",
    "cache_url": "CACHE_URL",
    "cache": "CACHE_URL",
    "email_url": "EMAIL_URL",
    "email": "EMAIL_URL",
    "search_url": "SEARCH_URL",
    "channels_url": "CHANNELS_URL",
    "channels": "CHANNELS_URL",
}

_ALIAS_KINDS = (
    "os",
    "getenv",
    "environ",
    "django_environ_mod",
    "environ_env_class",
    "environ_env",
    "decouple_config",
)
_MAX_FLOW_PASSES = 32


@dataclass
class _EnvironConfig:
    scheme: Dict[str, ast.AST] = field(default_factory=dict)
    # None means the prefix is computed dynamically.
    prefix: Optional[str] = ""


_Binding = Tuple[str, Optional[_EnvironConfig]]


class _FunctionBindingCollector(ast.NodeVisitor):
    """Find names Python treats as local throughout a function body."""

    def __init__(self) -> None:
        self.local_names: Set[str] = set()
        self.global_names: Set[str] = set()
        self.nonlocal_names: Set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.local_names.add(node.id)

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.local_names.add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.local_names.add(alias.asname or alias.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.local_names.add(node.name)
        self._visit_definition_expressions(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def _visit_definition_expressions(self, node) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        args = node.args
        for arg in [
            *getattr(args, "posonlyargs", []),
            *args.args,
            *args.kwonlyargs,
        ]:
            if arg.annotation is not None:
                self.visit(arg.annotation)
        if args.vararg is not None and args.vararg.annotation is not None:
            self.visit(args.vararg.annotation)
        if args.kwarg is not None and args.kwarg.annotation is not None:
            self.visit(args.kwarg.annotation)
        for default in args.defaults:
            self.visit(default)
        for default in args.kw_defaults:
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)
        for type_param in getattr(node, "type_params", []):
            self.visit(type_param)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.local_names.add(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for type_param in getattr(node, "type_params", []):
            self.visit(type_param)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        args = node.args
        for default in args.defaults:
            self.visit(default)
        for default in args.kw_defaults:
            if default is not None:
                self.visit(default)

    def _visit_comprehension(self, node, values: List[ast.AST]) -> None:
        for generator in node.generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node, [node.key, node.value])

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.local_names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node) -> None:
        if node.name is not None:
            self.local_names.add(node.name)
        if node.pattern is not None:
            self.visit(node.pattern)

    def visit_MatchStar(self, node) -> None:
        if node.name is not None:
            self.local_names.add(node.name)

    def visit_MatchMapping(self, node) -> None:
        if node.rest is not None:
            self.local_names.add(node.rest)
        for key in node.keys:
            self.visit(key)
        for pattern in node.patterns:
            self.visit(pattern)


class _EnvVisitor(ast.NodeVisitor):
    """Collect env usages while keeping track of lexical name bindings."""

    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.usages: List[EnvUsage] = []
        self._usage_keys: Set[Tuple[object, ...]] = set()
        self.scopes: List[dict] = [self._fresh_scope("module")]

    @staticmethod
    def _fresh_scope(kind: str) -> dict:
        return {
            "kind": kind,
            "os": set(),
            "getenv": set(),
            "environ": set(),
            "django_environ_mod": set(),
            "environ_env_class": set(),
            "environ_env": set(),
            "decouple_config": set(),
            "shadowed": set(),
            "environ_configs": {},
        }

    def _push_scope(self, kind: str) -> None:
        self.scopes.append(self._fresh_scope(kind))

    def _pop_scope(self) -> None:
        self.scopes.pop()

    def _visible_scopes(self) -> List[dict]:
        top = self.scopes[-1]
        visible = [top]
        can_see_class = top["kind"] == "annotation"
        for scope in reversed(self.scopes[:-1]):
            # A class namespace isn't a closure. Its own body sees its names,
            # but methods and nested classes don't resolve bare names from it.
            if scope["kind"] == "annotation":
                visible.append(scope)
                can_see_class = True
            elif scope["kind"] == "class":
                if can_see_class:
                    visible.append(scope)
                can_see_class = False
            else:
                visible.append(scope)
        return visible

    def _binding_kinds(self, name: str) -> Set[str]:
        for scope in self._visible_scopes():
            if name in scope["shadowed"]:
                return set()
            kinds = {kind for kind in _ALIAS_KINDS if name in scope[kind]}
            if kinds:
                return kinds
        return set()

    def _binding_kind(self, name: str) -> Optional[str]:
        kinds = self._binding_kinds(name)
        if len(kinds) == 1:
            return next(iter(kinds))
        return None

    def _is_alias(self, name: str, kind: str) -> bool:
        return kind in self._binding_kinds(name)

    def _bind(
        self,
        name: str,
        kind: Optional[str],
        config: Optional[_EnvironConfig] = None,
        scope_index: int = -1,
    ) -> None:
        scope = self.scopes[scope_index]
        for alias_kind in _ALIAS_KINDS:
            scope[alias_kind].discard(name)
        scope["shadowed"].discard(name)
        scope["environ_configs"].pop(name, None)

        if kind is None:
            scope["shadowed"].add(name)
        else:
            scope[kind].add(name)
            if kind == "environ_env":
                scope["environ_configs"][name] = config or _EnvironConfig()

    def _bind_options(
        self,
        name: str,
        bindings: List[_Binding],
        scope_index: int = -1,
    ) -> None:
        unique: List[_Binding] = []
        seen: Set[Tuple[str, Optional[int]]] = set()
        for kind, config in bindings:
            key = (kind, id(config) if config is not None else None)
            if key not in seen:
                seen.add(key)
                unique.append((kind, config))

        if not unique:
            self._bind(name, None, scope_index=scope_index)
            return
        if len(unique) == 1:
            kind, config = unique[0]
            self._bind(name, kind, config, scope_index=scope_index)
            return

        scope = self.scopes[scope_index]
        for alias_kind in _ALIAS_KINDS:
            scope[alias_kind].discard(name)
        scope["shadowed"].discard(name)
        scope["environ_configs"].pop(name, None)

        for kind, _ in unique:
            scope[kind].add(name)
        configs = [
            config for kind, config in unique
            if kind == "environ_env" and config is not None
        ]
        if configs:
            unique_configs = list({id(config): config for config in configs}.values())
            if len(unique_configs) == 1:
                scope["environ_configs"][name] = unique_configs[0]
            else:
                scope["environ_configs"][name] = self._combine_configs(unique_configs)

    def _env_config(self, name: str) -> Optional[_EnvironConfig]:
        for scope in self._visible_scopes():
            if name in scope["shadowed"]:
                return None
            if name in scope["environ_env"]:
                return scope["environ_configs"].get(name, _EnvironConfig())
            if any(name in scope[kind] for kind in _ALIAS_KINDS):
                return None
        return None

    def _clone_scopes(self) -> List[dict]:
        return copy.deepcopy(self.scopes)

    @staticmethod
    def _combine_configs(configs: List[_EnvironConfig]) -> _EnvironConfig:
        if not configs:
            return _EnvironConfig()

        prefixes = {config.prefix for config in configs}
        prefix = next(iter(prefixes)) if len(prefixes) == 1 else None
        common_keys = set(configs[0].scheme)
        for config in configs[1:]:
            common_keys.intersection_update(config.scheme)
        scheme = {key: configs[0].scheme[key] for key in common_keys}
        return _EnvironConfig(scheme=scheme, prefix=prefix)

    @staticmethod
    def _scopes_key(scopes: List[dict]) -> Tuple[object, ...]:
        result: List[object] = []
        for scope in scopes:
            aliases = tuple(
                (kind, tuple(sorted(scope[kind]))) for kind in _ALIAS_KINDS
            )
            config_groups: Dict[int, List[str]] = {}
            for name in scope["environ_env"]:
                config = scope["environ_configs"].get(name)
                if config is not None:
                    config_groups.setdefault(id(config), []).append(name)

            configs = []
            for names in config_groups.values():
                names_key = tuple(sorted(names))
                config = scope["environ_configs"][names_key[0]]
                scheme = tuple(
                    sorted(
                        (name, _unparse(default))
                        for name, default in config.scheme.items()
                    )
                )
                configs.append(
                    (names_key, config.prefix is None, config.prefix or "", scheme)
                )
            result.append(
                (
                    scope["kind"],
                    aliases,
                    tuple(sorted(scope["shadowed"])),
                    tuple(sorted(configs)),
                )
            )
        return tuple(result)

    def _merged_state(self, branches: List[List[dict]]) -> List[dict]:
        self._merge_branch_scopes(branches)
        return self._clone_scopes()

    def _merge_branch_scopes(self, branches: List[List[dict]]) -> None:
        if not branches:
            return

        merged_scopes: List[dict] = []
        for index in range(len(branches[0])):
            branch_scopes = [branch[index] for branch in branches]
            merged = self._fresh_scope(branch_scopes[0]["kind"])

            for kind in _ALIAS_KINDS:
                merged[kind] = set().union(
                    *(scope[kind] for scope in branch_scopes)
                )

            shadow_sets = [scope["shadowed"] for scope in branch_scopes]
            merged["shadowed"] = set.intersection(*shadow_sets)
            aliases = set().union(*(merged[kind] for kind in _ALIAS_KINDS))
            merged["shadowed"].difference_update(aliases)

            config_cache: Dict[Tuple[Optional[int], ...], _EnvironConfig] = {}
            for name in merged["environ_env"]:
                configs = [
                    scope["environ_configs"].get(name)
                    if name in scope["environ_env"] else None
                    for scope in branch_scopes
                ]
                signature = tuple(
                    id(config) if config is not None else None
                    for config in configs
                )
                if signature in config_cache:
                    merged["environ_configs"][name] = config_cache[signature]
                    continue

                present = [config for config in configs if config is not None]
                if len(present) != len(configs):
                    config = _EnvironConfig(prefix=None)
                else:
                    config = self._combine_configs(present)
                config_cache[signature] = config
                merged["environ_configs"][name] = config

            merged_scopes.append(merged)

        self.scopes = merged_scopes

    def _visit_branch(
        self,
        entry_scopes: List[dict],
        statements: List[ast.AST],
    ) -> List[dict]:
        self.scopes = copy.deepcopy(entry_scopes)
        for statement in statements:
            self.visit(statement)
        return self.scopes

    def _visit_statements_with_states(
        self,
        entry_scopes: List[dict],
        statements: List[ast.AST],
    ) -> Tuple[List[dict], List[List[dict]]]:
        self.scopes = copy.deepcopy(entry_scopes)
        states = [self._clone_scopes()]
        for statement in statements:
            self.visit(statement)
            states.append(self._clone_scopes())
        return self.scopes, states

    def _flow_fixpoint(
        self,
        initial: List[dict],
        step: Callable[[List[dict]], List[dict]],
        widen_names: Optional[Set[str]] = None,
    ) -> Tuple[List[dict], List[dict]]:
        possible = copy.deepcopy(initial)
        seen: Set[Tuple[object, ...]] = set()
        last_exit = copy.deepcopy(initial)
        for _ in range(_MAX_FLOW_PASSES):
            before = self._scopes_key(possible)
            if before in seen:
                return possible, last_exit
            seen.add(before)

            last_exit = step(possible)
            merged = self._merged_state([possible, last_exit])
            if self._scopes_key(merged) == before:
                return merged, last_exit
            possible = merged

        # A long reversed alias chain can otherwise make this quadratic. Once
        # the small exact fixpoint budget is exhausted, widen assigned names
        # and make one final pass so scanning stays fail-safe on hostile input.
        if widen_names:
            self.scopes = copy.deepcopy(possible)
            for name in sorted(widen_names):
                bindings: List[_Binding] = [
                    (kind, _EnvironConfig(prefix=None) if kind == "environ_env" else None)
                    for kind in _ALIAS_KINDS
                ]
                self._bind_options(name, bindings)
            possible = self._clone_scopes()
            last_exit = step(possible)
            return self._merged_state([possible, last_exit]), last_exit
        return possible, last_exit

    @staticmethod
    def _bound_names(nodes: List[ast.AST]) -> Set[str]:
        collector = _FunctionBindingCollector()
        for node in nodes:
            collector.visit(node)
        return collector.local_names

    # -------------------------------------------------------------- scopes

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_runtime_values(node)
        type_params = list(getattr(node, "type_params", []))
        if type_params:
            self._push_annotation_scope(type_params)
            try:
                for type_param in type_params:
                    self.visit(type_param)
                self._visit_function_annotations(node)
            finally:
                self._pop_scope()
        else:
            self._visit_function_annotations(node)

        self._bind(node.name, None)
        enclosing_scopes = self._clone_scopes()
        if type_params:
            self._push_annotation_scope(type_params)
        self._push_scope("function")
        try:
            self._prebind_function_locals(node.body)
            self._bind_arguments(node.args)
            for statement in node.body:
                self.visit(statement)
        finally:
            self.scopes = enclosing_scopes

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def _visit_function_runtime_values(self, node) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        args = node.args
        for default in args.defaults:
            self.visit(default)
        for default in args.kw_defaults:
            if default is not None:
                self.visit(default)

    def _visit_function_annotations(self, node) -> None:
        args = node.args
        annotated = [
            *getattr(args, "posonlyargs", []),
            *args.args,
            *args.kwonlyargs,
        ]
        if args.vararg is not None:
            annotated.append(args.vararg)
        if args.kwarg is not None:
            annotated.append(args.kwarg)
        for arg in annotated:
            if arg.annotation is not None:
                self.visit(arg.annotation)
        if node.returns is not None:
            self.visit(node.returns)

    def _push_annotation_scope(
        self,
        type_params: List[ast.AST],
        extra_names: Optional[List[str]] = None,
    ) -> None:
        self._push_scope("annotation")
        for name in extra_names or []:
            self._bind(name, None)
        for type_param in type_params:
            name = getattr(type_param, "name", None)
            if isinstance(name, str):
                self._bind(name, None)

    def _bind_arguments(self, args: ast.arguments) -> None:
        all_args = [
            *getattr(args, "posonlyargs", []),
            *args.args,
            *args.kwonlyargs,
        ]
        if args.vararg is not None:
            all_args.append(args.vararg)
        if args.kwarg is not None:
            all_args.append(args.kwarg)
        for arg in all_args:
            self._bind(arg.arg, None)

    def _prebind_function_locals(self, nodes: List[ast.AST]) -> None:
        collector = _FunctionBindingCollector()
        for node in nodes:
            collector.visit(node)
        nonlocal_names = collector.global_names | collector.nonlocal_names
        for name in collector.local_names - nonlocal_names:
            self._bind(name, None)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        args = node.args
        for arg in [
            *getattr(args, "posonlyargs", []),
            *args.args,
            *args.kwonlyargs,
        ]:
            if arg.annotation is not None:
                self.visit(arg.annotation)
        for default in args.defaults:
            self.visit(default)
        for default in args.kw_defaults:
            if default is not None:
                self.visit(default)
        enclosing_scopes = self._clone_scopes()
        self._push_scope("function")
        try:
            self._prebind_function_locals([node.body])
            self._bind_arguments(args)
            self.visit(node.body)
        finally:
            self.scopes = enclosing_scopes

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)

        type_params = list(getattr(node, "type_params", []))
        if type_params:
            self._push_annotation_scope(type_params)
        try:
            for type_param in type_params:
                self.visit(type_param)
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
            self._push_scope("class")
            try:
                for statement in node.body:
                    self.visit(statement)
            finally:
                self._pop_scope()
        finally:
            if type_params:
                self._pop_scope()
        self._bind(node.name, None)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        entry = self._clone_scopes()
        body_state = self._visit_branch(entry, node.body)
        if node.orelse:
            else_state = self._visit_branch(entry, node.orelse)
        else:
            else_state = copy.deepcopy(entry)
        self._merge_branch_scopes([body_state, else_state])

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.visit(node.test)
        entry = self._clone_scopes()

        self.scopes = copy.deepcopy(entry)
        self.visit(node.body)
        body_state = self.scopes

        self.scopes = copy.deepcopy(entry)
        self.visit(node.orelse)
        else_state = self.scopes
        self._merge_branch_scopes([body_state, else_state])

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if not node.values:
            return
        self.visit(node.values[0])
        for value in node.values[1:]:
            skipped_state = self._clone_scopes()
            self.visit(value)
            evaluated_state = self.scopes
            self._merge_branch_scopes([skipped_state, evaluated_state])

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node, handlers_can_chain=False)

    def _visit_handler_from(
        self,
        entry: List[dict],
        handler: ast.ExceptHandler,
    ) -> Tuple[List[dict], List[dict], List[List[dict]], List[List[dict]]]:
        self.scopes = copy.deepcopy(entry)
        all_states = [self._clone_scopes()]
        if handler.type is not None:
            self.visit(handler.type)
            all_states.append(self._clone_scopes())
        after_type = self._clone_scopes()

        self.scopes = copy.deepcopy(after_type)
        matching_states = [self._clone_scopes()]
        if handler.name is not None:
            self._bind(handler.name, None)
            matching_states.append(self._clone_scopes())
        for statement in handler.body:
            self.visit(statement)
            matching_states.append(self._clone_scopes())
        completed = self._clone_scopes()
        all_states.extend(matching_states)
        return after_type, completed, matching_states, all_states

    def _visit_try(self, node, handlers_can_chain: bool) -> None:
        entry = self._clone_scopes()
        try_normal_state, exception_states = self._visit_statements_with_states(
            entry, node.body
        )

        else_exception_states: List[List[dict]] = []
        if node.orelse:
            normal_state, else_exception_states = self._visit_statements_with_states(
                try_normal_state, node.orelse
            )
        else:
            normal_state = copy.deepcopy(try_normal_state)

        handler_entry = self._merged_state(exception_states)
        continuation_states = [normal_state]
        final_entries = [*exception_states, *else_exception_states]
        if handlers_can_chain:
            clause_state = handler_entry
            for handler in node.handlers:
                after_type, completed, matching_states, handler_states = (
                    self._visit_handler_from(clause_state, handler)
                )
                clause_state = self._merged_state(
                    [after_type, completed, *matching_states]
                )
                final_entries.extend(handler_states)
            continuation_states.append(clause_state)
            final_entries.append(clause_state)
        else:
            unmatched: Optional[List[dict]] = handler_entry
            for handler in node.handlers:
                if unmatched is None:
                    break
                after_type, completed, _, handler_states = self._visit_handler_from(
                    unmatched, handler
                )
                continuation_states.append(completed)
                final_entries.extend(handler_states)
                unmatched = after_type if handler.type is not None else None
            if unmatched is not None:
                final_entries.append(unmatched)

        if node.finalbody:
            self._merge_branch_scopes([*continuation_states, *final_entries])
            for statement in node.finalbody:
                self.visit(statement)
        else:
            self._merge_branch_scopes(continuation_states)

    def visit_TryStar(self, node) -> None:
        self._visit_try(node, handlers_can_chain=True)

    def visit_While(self, node: ast.While) -> None:
        initial = self._clone_scopes()
        after_test = copy.deepcopy(initial)

        def visit_iteration(head: List[dict]) -> List[dict]:
            nonlocal after_test
            self.scopes = copy.deepcopy(head)
            self.visit(node.test)
            after_test = self._clone_scopes()
            return self._visit_branch(after_test, node.body)

        widened = self._bound_names([node.test, *node.body])
        _, body_state = self._flow_fixpoint(
            initial, visit_iteration, widen_names=widened
        )
        self._merge_branch_scopes([after_test, body_state])
        before_else = self._clone_scopes()
        else_state = self._visit_branch(before_else, node.orelse)
        self._merge_branch_scopes([before_else, else_state])

    def visit_Match(self, node) -> None:
        self.visit(node.subject)
        remaining: Optional[List[dict]] = self._clone_scopes()
        completed_states: List[List[dict]] = []
        for case in node.cases:
            if remaining is None:
                break

            self.scopes = copy.deepcopy(remaining)
            self.visit(case.pattern)
            matched = self._clone_scopes()
            irrefutable = _is_irrefutable_pattern(case.pattern)
            if case.guard is not None:
                self.scopes = copy.deepcopy(matched)
                self.visit(case.guard)
                guarded = self._clone_scopes()
                completed_states.append(self._visit_branch(guarded, case.body))
                failed_guard_states = [guarded]
                if not irrefutable:
                    failed_guard_states.append(remaining)
                remaining = self._merged_state(failed_guard_states)
            else:
                completed_states.append(self._visit_branch(matched, case.body))
                if irrefutable:
                    remaining = None

        if remaining is not None:
            completed_states.append(remaining)
        if completed_states:
            self._merge_branch_scopes(completed_states)

    def visit_MatchAs(self, node) -> None:
        if node.pattern is not None:
            self.visit(node.pattern)
        if node.name is not None:
            self._bind(node.name, None)

    def visit_MatchStar(self, node) -> None:
        if node.name is not None:
            self._bind(node.name, None)

    def visit_MatchMapping(self, node) -> None:
        for key in node.keys:
            self.visit(key)
        for pattern in node.patterns:
            self.visit(pattern)
        if node.rest is not None:
            self._bind(node.rest, None)

    def visit_TypeAlias(self, node) -> None:
        type_params = list(node.type_params)
        target_name = node.name.id if isinstance(node.name, ast.Name) else None
        extras = [target_name] if target_name is not None else []
        self._push_annotation_scope(type_params, extra_names=extras)
        try:
            for type_param in type_params:
                self.visit(type_param)
            self.visit(node.value)
        finally:
            self._pop_scope()
        self._bind_target(node.name)

    def _visit_comprehension(self, node, values: List[ast.AST]) -> None:
        generators = node.generators
        if not generators:
            for value in values:
                self.visit(value)
            return

        # Python evaluates the first iterable outside the implicit comp scope.
        self.visit(generators[0].iter)
        zero_iterations = self._clone_scopes()

        def visit_iteration(head: List[dict]) -> List[dict]:
            self.scopes = copy.deepcopy(head)
            self._push_scope("comprehension")
            try:
                first = generators[0]
                self.visit(first.target)
                self._bind_target(first.target)
                for condition in first.ifs:
                    self.visit(condition)
                for generator in generators[1:]:
                    self.visit(generator.iter)
                    self.visit(generator.target)
                    self._bind_target(generator.target)
                    for condition in generator.ifs:
                        self.visit(condition)
                for value in values:
                    self.visit(value)
            finally:
                self._pop_scope()
            return self._clone_scopes()

        repeated_nodes = [*generators[0].ifs, *values]
        for generator in generators[1:]:
            repeated_nodes.extend([generator.iter, *generator.ifs])
        widened = self._bound_names(repeated_nodes)
        possible, _ = self._flow_fixpoint(
            zero_iterations, visit_iteration, widen_names=widened
        )
        self.scopes = possible

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node, [node.key, node.value])

    # -------------------------------------------------------------- bindings

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            if alias.name == "os" or (
                alias.name == "os.path" and alias.asname is None
            ):
                self._bind(bound, "os")
            elif alias.name == "environ":
                self._bind(bound, "django_environ_mod")
            else:
                self._bind(bound, None)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            for alias in node.names:
                self._bind(alias.asname or alias.name, None)
            return

        for alias in node.names:
            bound = alias.asname or alias.name
            if node.module == "os" and alias.name == "*":
                self._bind("getenv", "getenv")
                self._bind("environ", "environ")
                continue
            kind = None
            if node.module == "os" and alias.name == "getenv":
                kind = "getenv"
            elif node.module == "os" and alias.name == "environ":
                kind = "environ"
            elif node.module == "decouple" and alias.name == "config":
                kind = "decouple_config"
            elif node.module == "environ" and alias.name in {"Env", "FileAwareEnv"}:
                kind = "environ_env_class"
            self._bind(bound, kind)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self.visit(target)
        bindings = self._value_bindings(node.value)
        for target in node.targets:
            self._bind_assignment_target(target, bindings)
            self._set_env_prefix(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.target)
        bindings = self._value_bindings(node.value)
        self._bind_assignment_target(node.target, bindings)
        if node.value is not None:
            self._set_env_prefix(node.target, node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self.visit(node.target)
        scope_index = -1
        if self.scopes[-1]["kind"] == "comprehension":
            for index in range(len(self.scopes) - 2, -1, -1):
                if self.scopes[index]["kind"] != "comprehension":
                    scope_index = index
                    break
        self._bind_assignment_target(
            node.target,
            self._value_bindings(node.value),
            scope_index=scope_index,
        )

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        # The target has Store context in the AST, but += reads before writing.
        if isinstance(node.target, ast.Subscript) and self._is_environ_subscript(node.target):
            self._record_subscript(node.target)
        self.visit(node.target)
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self._bind(node.target.id, None)
        elif isinstance(node.target, ast.Attribute) and node.target.attr == "prefix":
            config = self._attribute_env_config(node.target)
            suffix = _extract_string(node.value)
            if (
                config is not None
                and isinstance(node.op, ast.Add)
                and config.prefix is not None
                and suffix is not None
            ):
                config.prefix += suffix
            elif config is not None:
                config.prefix = None

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self.visit(target)
            self._bind_target(target)
            if isinstance(target, ast.Attribute) and target.attr == "prefix":
                config = self._attribute_env_config(target)
                if config is not None:
                    config.prefix = None

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        zero_iterations = self._clone_scopes()

        def visit_iteration(head: List[dict]) -> List[dict]:
            self.scopes = copy.deepcopy(head)
            self.visit(node.target)
            self._bind_target(node.target)
            for statement in node.body:
                self.visit(statement)
            return self._clone_scopes()

        widened = self._bound_names([node.target, *node.body])
        possible, _ = self._flow_fixpoint(
            zero_iterations, visit_iteration, widen_names=widened
        )
        self.scopes = possible
        before_else = self._clone_scopes()
        else_state = self._visit_branch(before_else, node.orelse)
        # A break skips the else suite, while normal and zero-iteration paths
        # execute it. Keeping both is conservative without building a CFG.
        self._merge_branch_scopes([before_else, else_state])

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)  # type: ignore[arg-type]

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
                self._bind_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)  # type: ignore[arg-type]

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None:
            self._bind(node.name, None)
        for statement in node.body:
            self.visit(statement)

    def _bind_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self._bind(target.id, None)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind_target(element)
        elif isinstance(target, ast.Starred):
            self._bind_target(target.value)

    def _bind_assignment_target(
        self,
        target: ast.AST,
        bindings: List[_Binding],
        scope_index: int = -1,
    ) -> None:
        if isinstance(target, ast.Name):
            self._bind_options(target.id, bindings, scope_index=scope_index)
        else:
            self._bind_target(target)

    def _attribute_env_config(
        self, target: ast.Attribute
    ) -> Optional[_EnvironConfig]:
        if not isinstance(target.value, ast.Name):
            return None
        return self._env_config(target.value.id)

    def _set_env_prefix(self, target: ast.AST, value: ast.AST) -> None:
        if not isinstance(target, ast.Attribute) or target.attr != "prefix":
            return
        config = self._attribute_env_config(target)
        if config is not None:
            config.prefix = _extract_string(value)

    def _value_bindings(self, value: Optional[ast.AST]) -> List[_Binding]:
        if value is None:
            return []
        if isinstance(value, ast.Name):
            bindings = []
            for kind in sorted(self._binding_kinds(value.id)):
                config = self._env_config(value.id) if kind == "environ_env" else None
                bindings.append((kind, config))
            return bindings
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            owner = value.value.id
            bindings = []
            if self._is_alias(owner, "os"):
                if value.attr == "getenv":
                    bindings.append(("getenv", None))
                elif value.attr == "environ":
                    bindings.append(("environ", None))
            if (
                self._is_alias(owner, "django_environ_mod")
                and value.attr in {"Env", "FileAwareEnv"}
            ):
                bindings.append(("environ_env_class", None))
            return bindings
        if isinstance(value, ast.Call) and self._is_env_constructor(value):
            return [("environ_env", self._constructor_config(value))]
        if isinstance(value, ast.IfExp):
            return [
                *self._value_bindings(value.body),
                *self._value_bindings(value.orelse),
            ]
        if isinstance(value, ast.BoolOp):
            return [
                binding
                for part in value.values
                for binding in self._value_bindings(part)
            ]
        if isinstance(value, ast.NamedExpr):
            return self._value_bindings(value.value)
        return []

    def _is_env_constructor(self, call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            return self._is_alias(func.id, "environ_env_class")
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr in {"Env", "FileAwareEnv"}:
            return isinstance(func.value, ast.Name) and self._is_alias(
                func.value.id, "django_environ_mod"
            )
        if func.attr != "configured":
            return False
        owner = func.value
        if isinstance(owner, ast.Name):
            return self._is_alias(owner.id, "environ_env_class")
        return (
            isinstance(owner, ast.Attribute)
            and owner.attr in {"Env", "FileAwareEnv"}
            and isinstance(owner.value, ast.Name)
            and self._is_alias(owner.value.id, "django_environ_mod")
        )

    def _constructor_config(self, call: ast.Call) -> _EnvironConfig:
        schema: Dict[str, ast.AST] = {}
        is_configured = isinstance(call.func, ast.Attribute) and call.func.attr == "configured"
        if is_configured:
            scheme_node = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "scheme"),
                None,
            )
            if scheme_node is None and len(call.args) > 1:
                scheme_node = call.args[1]
            if isinstance(scheme_node, ast.Dict):
                for key, value in zip(scheme_node.keys, scheme_node.values):
                    name = _extract_string(key)
                    default = _schema_default(value)
                    if name is not None and default is not None:
                        schema[name] = default
            prefix_node = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "prefix"),
                None,
            )
            prefix = "" if prefix_node is None else _extract_string(prefix_node)
            return _EnvironConfig(scheme=schema, prefix=prefix)

        for keyword in call.keywords:
            if keyword.arg is None:
                continue
            default = _schema_default(keyword.value)
            if default is not None:
                schema[keyword.arg] = default
        return _EnvironConfig(scheme=schema)

    # --------------------------------------------------------------- matches

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_getenv_call(node):
            self._record_call(node, "getenv", name_keywords=("key",))
        elif self._is_environ_get_call(node):
            self._record_call(node, "environ_get", name_keywords=("key",))
        else:
            django = self._django_environ_kind(node)
            if django is not None:
                call_type, instance, method = django
                config = self._env_config(instance) or _EnvironConfig()
                default_position = (
                    2 if method is None else _DJANGO_DEFAULT_POSITIONS[method]
                )
                implicit_name = (
                    None if method is None else _DJANGO_IMPLICIT_NAMES.get(method)
                )
                self._record_call(
                    node,
                    call_type,
                    name_keywords=("var",),
                    default_position=default_position,
                    implicit_name=implicit_name,
                    schema=config.scheme,
                    name_prefix=config.prefix,
                    prefix_expr=f"{instance}.prefix",
                )
            elif self._is_decouple_config_call(node):
                self._record_call(
                    node,
                    "decouple_config",
                    name_keywords=("option",),
                )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load) and self._is_environ_subscript(node):
            self._record_subscript(node)
        self.generic_visit(node)

    def _is_getenv_call(self, node: ast.Call) -> bool:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "getenv":
            return isinstance(func.value, ast.Name) and self._is_alias(
                func.value.id, "os"
            )
        return isinstance(func, ast.Name) and self._is_alias(func.id, "getenv")

    def _is_environ_get_call(self, node: ast.Call) -> bool:
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "get":
            return False
        owner = func.value
        if isinstance(owner, ast.Attribute) and owner.attr == "environ":
            return isinstance(owner.value, ast.Name) and self._is_alias(
                owner.value.id, "os"
            )
        return isinstance(owner, ast.Name) and self._is_alias(owner.id, "environ")

    def _is_environ_subscript(self, node: ast.Subscript) -> bool:
        owner = node.value
        if isinstance(owner, ast.Attribute) and owner.attr == "environ":
            return isinstance(owner.value, ast.Name) and self._is_alias(
                owner.value.id, "os"
            )
        return isinstance(owner, ast.Name) and self._is_alias(owner.id, "environ")

    def _django_environ_kind(
        self, node: ast.Call
    ) -> Optional[Tuple[str, str, Optional[str]]]:
        func = node.func
        if isinstance(func, ast.Name) and self._is_alias(func.id, "environ_env"):
            return "django_environ", func.id, None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if (
                self._is_alias(func.value.id, "environ_env")
                and func.attr in _DJANGO_DEFAULT_POSITIONS
            ):
                return f"django_environ.{func.attr}", func.value.id, func.attr
        return None

    def _is_decouple_config_call(self, node: ast.Call) -> bool:
        return isinstance(node.func, ast.Name) and self._is_alias(
            node.func.id, "decouple_config"
        )

    # -------------------------------------------------------------- recording

    def _append_usage(self, node: ast.AST, usage: EnvUsage) -> None:
        default_expr = (
            _unparse(usage.default_node) if usage.default_node is not None else None
        )
        key = (
            id(node),
            usage.name,
            usage.call_type,
            usage.raw_expr,
            usage.has_default,
            default_expr,
        )
        if key in self._usage_keys:
            return
        self._usage_keys.add(key)
        self.usages.append(usage)

    def _record_call(
        self,
        node: ast.Call,
        call_type: str,
        *,
        name_keywords: Tuple[str, ...],
        default_position: int = 1,
        implicit_name: Optional[str] = None,
        schema: Optional[Dict[str, ast.AST]] = None,
        name_prefix: Optional[str] = "",
        prefix_expr: str = "prefix",
    ) -> None:
        name_arg = node.args[0] if node.args else None
        if name_arg is None:
            name_arg = next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg in name_keywords
                ),
                None,
            )
        if name_arg is None and implicit_name is None:
            return

        default_node: Optional[ast.AST] = None
        if len(node.args) > default_position:
            default_node = node.args[default_position]
        if default_node is None:
            default_node = next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "default"
                ),
                None,
            )

        base_name = implicit_name if name_arg is None else _extract_string(name_arg)
        raw_name = _unparse(name_arg) if name_arg is not None else repr(implicit_name)
        if base_name is not None and name_prefix is not None:
            name = f"{name_prefix}{base_name}"
            raw_expr = None
        else:
            name = None
            if name_prefix:
                raw_expr = f"{name_prefix!r} + ({raw_name})"
            elif name_prefix is None:
                raw_expr = f"{prefix_expr} + ({raw_name})"
            else:
                raw_expr = raw_name
        if default_node is None and name is not None and schema is not None:
            default_node = schema.get(name)
        self._append_usage(
            node,
            EnvUsage(
                name=name,
                file=self.file_path,
                line=node.lineno,
                has_default=default_node is not None,
                call_type=call_type,
                raw_expr=raw_expr,
                default_node=default_node,
            ),
        )

    def _record_subscript(self, node: ast.Subscript) -> None:
        key_node = _subscript_key(node)
        name = _extract_string(key_node)
        raw = None if name is not None else _unparse(key_node)
        self._append_usage(
            node,
            EnvUsage(
                name=name,
                file=self.file_path,
                line=node.lineno,
                has_default=False,
                call_type="environ_subscript",
                raw_expr=raw,
            ),
        )


def _schema_default(node: ast.AST) -> Optional[ast.AST]:
    if isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == 2:
        return node.elts[1]
    return None


def _is_irrefutable_pattern(node: ast.AST) -> bool:
    match_as = getattr(ast, "MatchAs", ())
    if isinstance(node, match_as):
        return node.pattern is None or _is_irrefutable_pattern(node.pattern)
    match_or = getattr(ast, "MatchOr", ())
    return isinstance(node, match_or) and any(
        _is_irrefutable_pattern(pattern) for pattern in node.patterns
    )


def _extract_string(node: Optional[ast.AST]) -> Optional[str]:
    """Return a string literal's value, or None for a dynamic expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _subscript_key(node: ast.Subscript) -> Optional[ast.AST]:
    key = node.slice
    Index = getattr(ast, "Index", None)
    if Index is not None and isinstance(key, Index):
        return key.value  # type: ignore[attr-defined]
    return key


def _unparse(node: Optional[ast.AST]) -> str:
    if node is None:
        return "?"
    unparse = getattr(ast, "unparse", None)
    if unparse is not None:
        try:
            return unparse(node)
        except Exception:
            pass
    return f"<expr at line {getattr(node, 'lineno', '?')}>"


def scan_file(path: Path) -> List[EnvUsage]:
    """Scan one Python source file."""
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            raise ScanError(
                f"skipped {path}: file is larger than {MAX_FILE_SIZE} bytes"
            )
    except OSError as exc:
        raise ScanError(f"could not stat {path}: {exc}") from exc

    try:
        # tokenize.open implements Python's PEP 263 encoding-cookie rules and
        # also handles the UTF-8 BOM used by some Windows editors.
        with tokenize.open(path) as source_file:
            source = source_file.read()
    except (OSError, UnicodeDecodeError, LookupError, SyntaxError) as exc:
        raise ScanError(f"could not read {path}: {exc}") from exc

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ScanError(f"syntax error in {path}: {exc}") from exc
    except RecursionError as exc:
        raise ScanError(f"could not scan {path}: AST is too deeply nested") from exc

    visitor = _EnvVisitor(path)
    try:
        visitor.visit(tree)
    except RecursionError as exc:
        raise ScanError(f"could not scan {path}: AST is too deeply nested") from exc
    return visitor.usages


DEFAULT_EXCLUDES = frozenset({
    "venv", ".venv", "env",
    "__pycache__", ".git", ".hg", ".svn",
    "node_modules",
    ".tox", ".nox",
    "build", "dist",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".eggs",
})

DEFAULT_EXTENSIONS = frozenset({".py"})
MAX_FILE_SIZE = 2 * 1024 * 1024


def iter_python_files(
    root: Path,
    extensions: Optional[Set[str]] = None,
    extra_excludes: Optional[Set[str]] = None,
) -> List[Path]:
    """Return matching files while pruning excluded directories during walk."""
    exts = {extension.casefold() for extension in (extensions or DEFAULT_EXTENSIONS)}
    excludes = {name.casefold() for name in DEFAULT_EXCLUDES}
    if extra_excludes:
        excludes.update(name.casefold() for name in extra_excludes)

    try:
        mode = root.stat().st_mode
    except OSError as exc:
        raise ScanError(f"could not access scan path {root}: {exc}") from exc

    if stat.S_ISREG(mode):
        return [root] if root.suffix.casefold() in exts else []
    if not stat.S_ISDIR(mode):
        raise ScanError(f"scan path is not a file or directory: {root}")

    def walk_error(exc: OSError) -> None:
        raise ScanError(f"could not walk {root}: {exc}") from exc

    files: List[Path] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, onerror=walk_error):
        current_path = Path(current)
        kept_directories = []
        for dirname in dirnames:
            if dirname.casefold() in excludes:
                continue
            try:
                is_virtualenv = (
                    current_path / dirname / "pyvenv.cfg"
                ).is_file()
            except OSError:
                is_virtualenv = False
            if not is_virtualenv:
                kept_directories.append(dirname)
        dirnames[:] = kept_directories
        for filename in filenames:
            path = current_path / filename
            if path.suffix.casefold() in exts:
                files.append(path)
    return sorted(files, key=lambda path: os.path.normcase(str(path)))


def scan_project(
    root: Path,
    extensions: Optional[Set[str]] = None,
    extra_excludes: Optional[Set[str]] = None,
    on_file: Optional[Callable[[Path], None]] = None,
    files: Optional[List[Path]] = None,
) -> ScanResult:
    """Scan Python files below ``root`` and aggregate results and errors.

    A caller that already enumerated files (for example, to size a progress
    bar) may pass ``files`` to avoid traversing the project twice.
    """
    result = ScanResult()
    if files is None:
        try:
            scan_files = iter_python_files(
                root,
                extensions=extensions,
                extra_excludes=extra_excludes,
            )
        except ScanError as exc:
            result.errors.append((root, str(exc)))
            return result
    else:
        scan_files = list(files)

    result.scanned_files = scan_files
    for path in scan_files:
        try:
            result.usages.extend(scan_file(path))
        except ScanError as exc:
            result.errors.append((path, str(exc)))
        if on_file is not None:
            on_file(path)
    return result
