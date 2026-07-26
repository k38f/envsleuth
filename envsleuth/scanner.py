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
from typing import Callable, Dict, List, Optional, Set, Tuple, Union


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

    accepted_names: Tuple[str, ...] = ()
    """All accepted names when one field can read several env variables."""

    case_sensitive: Optional[bool] = None
    """None uses platform matching; Pydantic settings provide an explicit mode."""

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
    "pydantic_settings_mod",
    "pydantic_mod",
    "pydantic_settings_base",
    "pydantic_settings_config",
    "pydantic_field",
    "pydantic_alias_choices",
)
_MAX_FLOW_PASSES = 32


@dataclass
class _EnvironConfig:
    scheme: Dict[str, ast.AST] = field(default_factory=dict)
    # None means the prefix is computed dynamically.
    prefix: Optional[str] = ""


@dataclass
class _PydanticField:
    node: ast.AnnAssign
    field_name: str
    alias_names: Optional[Tuple[str, ...]]
    raw_alias: Optional[str]
    has_default: bool
    default_node: Optional[ast.AST]


@dataclass
class _PydanticSettingsConfig:
    # None means the value is computed dynamically.
    prefix: Optional[str] = ""
    prefix_target: Optional[str] = "variable"
    case_sensitive: Optional[bool] = False
    aliases_dynamic: bool = False
    fields: Tuple[_PydanticField, ...] = ()

    def __deepcopy__(self, memo: dict) -> "_PydanticSettingsConfig":
        clone = _PydanticSettingsConfig(
            prefix=self.prefix,
            prefix_target=self.prefix_target,
            case_sensitive=self.case_sensitive,
            aliases_dynamic=self.aliases_dynamic,
            # Field nodes are immutable scanner input. Copying whole AST
            # subtrees on every control-flow branch gets expensive quickly.
            fields=self.fields,
        )
        memo[id(self)] = clone
        return clone


_BindingConfig = Union[_EnvironConfig, _PydanticSettingsConfig]
_Binding = Tuple[str, Optional[_BindingConfig]]


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
        scope = {
            "kind": kind,
            "shadowed": set(),
            "environ_configs": {},
            "pydantic_configs": {},
        }
        scope.update({alias_kind: set() for alias_kind in _ALIAS_KINDS})
        return scope

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
        config: Optional[_BindingConfig] = None,
        scope_index: int = -1,
    ) -> None:
        scope = self.scopes[scope_index]
        for alias_kind in _ALIAS_KINDS:
            scope[alias_kind].discard(name)
        scope["shadowed"].discard(name)
        scope["environ_configs"].pop(name, None)
        scope["pydantic_configs"].pop(name, None)

        if kind is None:
            scope["shadowed"].add(name)
        else:
            scope[kind].add(name)
            if kind == "environ_env":
                scope["environ_configs"][name] = (
                    config if isinstance(config, _EnvironConfig)
                    else _EnvironConfig()
                )
            elif kind == "pydantic_settings_base":
                scope["pydantic_configs"][name] = (
                    config if isinstance(config, _PydanticSettingsConfig)
                    else _PydanticSettingsConfig()
                )

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
        scope["pydantic_configs"].pop(name, None)

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
        pydantic_configs = [
            config for kind, config in unique
            if (
                kind == "pydantic_settings_base"
                and isinstance(config, _PydanticSettingsConfig)
            )
        ]
        if pydantic_configs:
            unique_configs = list(
                {id(config): config for config in pydantic_configs}.values()
            )
            if len(unique_configs) == 1:
                scope["pydantic_configs"][name] = unique_configs[0]
            else:
                scope["pydantic_configs"][name] = (
                    self._combine_pydantic_configs(unique_configs)
                )

    def _env_config(self, name: str) -> Optional[_EnvironConfig]:
        for scope in self._visible_scopes():
            if name in scope["shadowed"]:
                return None
            if name in scope["environ_env"]:
                return scope["environ_configs"].get(name, _EnvironConfig())
            if any(name in scope[kind] for kind in _ALIAS_KINDS):
                return None
        return None

    def _pydantic_config(
        self, name: str
    ) -> Optional[_PydanticSettingsConfig]:
        for scope in self._visible_scopes():
            if name in scope["shadowed"]:
                return None
            if name in scope["pydantic_settings_base"]:
                return scope["pydantic_configs"].get(
                    name, _PydanticSettingsConfig()
                )
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
    def _combine_pydantic_configs(
        configs: List[_PydanticSettingsConfig],
    ) -> _PydanticSettingsConfig:
        if not configs:
            return _PydanticSettingsConfig()
        prefixes = {config.prefix for config in configs}
        prefix_targets = {config.prefix_target for config in configs}
        case_modes = {config.case_sensitive for config in configs}
        fields: List[_PydanticField] = []
        field_keys: Set[Tuple[object, ...]] = set()
        for config in configs:
            for field_info in config.fields:
                key = _pydantic_field_key(field_info)
                if key not in field_keys:
                    field_keys.add(key)
                    fields.append(field_info)
        return _PydanticSettingsConfig(
            prefix=next(iter(prefixes)) if len(prefixes) == 1 else None,
            prefix_target=(
                next(iter(prefix_targets))
                if len(prefix_targets) == 1 else None
            ),
            case_sensitive=(
                next(iter(case_modes)) if len(case_modes) == 1 else None
            ),
            aliases_dynamic=any(config.aliases_dynamic for config in configs),
            fields=tuple(fields),
        )

    @staticmethod
    def _inherit_pydantic_configs(
        configs: List[_PydanticSettingsConfig],
    ) -> _PydanticSettingsConfig:
        if not configs:
            return _PydanticSettingsConfig()

        # BaseSettings carries its settings defaults in model_config, so the
        # rightmost base replaces those values as well as direct overrides.
        config = configs[-1]
        fields_by_name: Dict[str, _PydanticField] = {}
        order: List[str] = []
        for base_config in reversed(configs):
            for field_info in base_config.fields:
                if field_info.field_name not in fields_by_name:
                    order.append(field_info.field_name)
                fields_by_name[field_info.field_name] = field_info
        return _PydanticSettingsConfig(
            prefix=config.prefix,
            prefix_target=config.prefix_target,
            case_sensitive=config.case_sensitive,
            aliases_dynamic=config.aliases_dynamic,
            fields=tuple(fields_by_name[name] for name in order),
        )

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
            pydantic_config_groups: Dict[int, List[str]] = {}
            for name in scope["pydantic_settings_base"]:
                config = scope["pydantic_configs"].get(name)
                if config is not None:
                    pydantic_config_groups.setdefault(id(config), []).append(name)

            pydantic_configs = []
            for names in pydantic_config_groups.values():
                names_key = tuple(sorted(names))
                config = scope["pydantic_configs"][names_key[0]]
                pydantic_configs.append(
                    (
                        names_key,
                        config.prefix,
                        config.prefix_target,
                        config.case_sensitive,
                        config.aliases_dynamic,
                        tuple(
                            _pydantic_field_key(field_info)
                            for field_info in config.fields
                        ),
                    )
                )
            result.append(
                (
                    scope["kind"],
                    aliases,
                    tuple(sorted(scope["shadowed"])),
                    tuple(sorted(configs)),
                    tuple(sorted(pydantic_configs)),
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

            pydantic_cache: Dict[
                Tuple[Optional[int], ...], _PydanticSettingsConfig
            ] = {}
            for name in merged["pydantic_settings_base"]:
                configs = [
                    scope["pydantic_configs"].get(name)
                    if name in scope["pydantic_settings_base"] else None
                    for scope in branch_scopes
                ]
                signature = tuple(
                    id(config) if config is not None else None
                    for config in configs
                )
                if signature in pydantic_cache:
                    merged["pydantic_configs"][name] = pydantic_cache[signature]
                    continue

                present = [
                    config for config in configs
                    if isinstance(config, _PydanticSettingsConfig)
                ]
                if len(present) != len(configs):
                    config = _PydanticSettingsConfig(
                        prefix=None,
                        prefix_target=None,
                        case_sensitive=None,
                        aliases_dynamic=True,
                        fields=self._combine_pydantic_configs(present).fields,
                    )
                else:
                    config = self._combine_pydantic_configs(present)
                pydantic_cache[signature] = config
                merged["pydantic_configs"][name] = config

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
                    (
                        kind,
                        _EnvironConfig(prefix=None)
                        if kind == "environ_env"
                        else (
                            _PydanticSettingsConfig(
                                prefix=None,
                                prefix_target=None,
                                case_sensitive=None,
                                aliases_dynamic=True,
                            )
                            if kind == "pydantic_settings_base"
                            else None
                        ),
                    )
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

        settings_config: Optional[_PydanticSettingsConfig] = None
        type_params = list(getattr(node, "type_params", []))
        if type_params:
            self._push_annotation_scope(type_params)
        try:
            for type_param in type_params:
                self.visit(type_param)
            base_configs = self._pydantic_base_configs(node.bases)
            pydantic_fields: List[_PydanticField] = []
            if base_configs:
                settings_config, pydantic_fields = (
                    self._analyze_pydantic_settings_class(node, base_configs)
                )
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
        if settings_config is None:
            self._bind(node.name, None)
        else:
            self._record_pydantic_fields(pydantic_fields, settings_config)
            self._bind(node.name, "pydantic_settings_base", settings_config)

    def _pydantic_base_configs(
        self, bases: List[ast.expr]
    ) -> List[_PydanticSettingsConfig]:
        configs = []
        for base in bases:
            for kind, config in self._value_bindings(base):
                if kind != "pydantic_settings_base":
                    continue
                if isinstance(config, _PydanticSettingsConfig):
                    configs.append(config)
                else:
                    configs.append(_PydanticSettingsConfig())
        return configs

    def _analyze_pydantic_settings_class(
        self,
        node: ast.ClassDef,
        base_configs: List[_PydanticSettingsConfig],
    ) -> Tuple[_PydanticSettingsConfig, List[_PydanticField]]:
        inherited = self._inherit_pydantic_configs(base_configs)
        config = self._copy_pydantic_config(inherited)
        direct_fields: List[_PydanticField] = []
        has_custom_sources = False
        self._push_scope("class")
        try:
            for statement in node.body:
                mutates_model_config = _mutates_pydantic_model_config(
                    statement
                )
                if isinstance(statement, ast.Import):
                    self.visit_Import(statement)
                    continue
                if isinstance(statement, ast.ImportFrom):
                    self.visit_ImportFrom(statement)
                    continue

                if isinstance(statement, ast.AnnAssign):
                    field_info = self._pydantic_field_info(statement)
                    if field_info is not None:
                        direct_fields.append(field_info)
                    if (
                        isinstance(statement.target, ast.Name)
                        and statement.target.id == "model_config"
                        and statement.value is not None
                    ):
                        # Only the last class namespace assignment survives.
                        config = self._copy_pydantic_config(inherited)
                        self._apply_pydantic_config(config, statement.value)
                    if statement.value is not None:
                        self._bind_assignment_target(
                            statement.target,
                            self._value_bindings(statement.value),
                        )
                    if mutates_model_config:
                        self._make_pydantic_config_dynamic(config)
                    continue

                if isinstance(statement, ast.Assign):
                    if any(
                        isinstance(target, ast.Name)
                        and target.id == "model_config"
                        for target in statement.targets
                    ):
                        config = self._copy_pydantic_config(inherited)
                        self._apply_pydantic_config(config, statement.value)
                    bindings = self._value_bindings(statement.value)
                    for target in statement.targets:
                        self._bind_assignment_target(target, bindings)
                    if mutates_model_config:
                        self._make_pydantic_config_dynamic(config)
                    continue

                collector = _FunctionBindingCollector()
                collector.visit(statement)
                if "model_config" in collector.local_names:
                    config = self._copy_pydantic_config(inherited)
                    self._make_pydantic_config_dynamic(config)
                elif mutates_model_config:
                    self._make_pydantic_config_dynamic(config)

                if isinstance(
                    statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    if (
                        isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and statement.name in {
                            "settings_customise_sources",
                            "customise_sources",
                        }
                    ):
                        has_custom_sources = True
                    self._bind(statement.name, None)
                elif isinstance(statement, ast.Delete):
                    for target in statement.targets:
                        self._bind_target(target)
                elif isinstance(statement, ast.AugAssign):
                    self._bind_target(statement.target)
        finally:
            self._pop_scope()

        # Pydantic class keywords take precedence over model_config.
        for keyword in node.keywords:
            if keyword.arg == "env_prefix":
                config.prefix = _extract_string(keyword.value)
            elif keyword.arg == "env_prefix_target":
                config.prefix_target = _extract_prefix_target(keyword.value)
            elif keyword.arg == "case_sensitive":
                config.case_sensitive = _extract_bool(keyword.value)
            elif keyword.arg == "alias_generator":
                config.aliases_dynamic = not _is_none_literal(keyword.value)
            elif keyword.arg in {"populate_by_name", "validate_by_name"}:
                if _extract_bool(keyword.value) is not False:
                    self._make_pydantic_config_dynamic(config)
            elif keyword.arg == "env_ignore_empty":
                if _extract_bool(keyword.value) is not False:
                    self._make_pydantic_config_dynamic(config)
            elif keyword.arg == "env_nested_delimiter":
                if not _is_none_literal(keyword.value):
                    self._make_pydantic_config_dynamic(config)

        if has_custom_sources:
            self._make_pydantic_config_dynamic(config)

        fields_by_name = {
            field_info.field_name: field_info
            for field_info in inherited.fields
        }
        for field_info in direct_fields:
            fields_by_name[field_info.field_name] = field_info
        fields = list(fields_by_name.values())
        config.fields = tuple(fields)
        return config, fields

    @staticmethod
    def _copy_pydantic_config(
        config: _PydanticSettingsConfig,
    ) -> _PydanticSettingsConfig:
        return _PydanticSettingsConfig(
            prefix=config.prefix,
            prefix_target=config.prefix_target,
            case_sensitive=config.case_sensitive,
            aliases_dynamic=config.aliases_dynamic,
            fields=config.fields,
        )

    @staticmethod
    def _make_pydantic_config_dynamic(
        config: _PydanticSettingsConfig,
    ) -> None:
        config.prefix = None
        config.prefix_target = None
        config.case_sensitive = None
        config.aliases_dynamic = True

    def _apply_pydantic_config(
        self,
        config: _PydanticSettingsConfig,
        value: ast.AST,
    ) -> None:
        values: Dict[str, ast.AST] = {}
        if self._is_pydantic_config_call(value):
            assert isinstance(value, ast.Call)
            unpacked = (
                len(value.args) > 1
                or (
                    bool(value.args)
                    and (
                        not isinstance(value.args[0], ast.Dict)
                        or _dict_has_unpack(value.args[0])
                    )
                )
                or any(keyword.arg is None for keyword in value.keywords)
            )
            if unpacked:
                self._make_pydantic_config_dynamic(config)
                return
            if value.args:
                assert isinstance(value.args[0], ast.Dict)
                values.update(_literal_string_dict(value.args[0]))
            for keyword in value.keywords:
                if keyword.arg is not None:
                    values[keyword.arg] = keyword.value
        elif isinstance(value, ast.Dict):
            if _dict_has_unpack(value):
                self._make_pydantic_config_dynamic(config)
                return
            values.update(_literal_string_dict(value))
        else:
            self._make_pydantic_config_dynamic(config)
            return

        if "env_prefix" in values:
            config.prefix = _extract_string(values["env_prefix"])
        if "env_prefix_target" in values:
            config.prefix_target = _extract_prefix_target(
                values["env_prefix_target"]
            )
        if "case_sensitive" in values:
            config.case_sensitive = _extract_bool(values["case_sensitive"])
        if "alias_generator" in values:
            config.aliases_dynamic = not _is_none_literal(
                values["alias_generator"]
            )
        if any(
            key in values and _extract_bool(values[key]) is not False
            for key in ("populate_by_name", "validate_by_name", "env_ignore_empty")
        ):
            self._make_pydantic_config_dynamic(config)
        if (
            "env_nested_delimiter" in values
            and not _is_none_literal(values["env_nested_delimiter"])
        ):
            self._make_pydantic_config_dynamic(config)

    def _is_pydantic_config_call(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Name):
            return self._is_alias(func.id, "pydantic_settings_config")
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "SettingsConfigDict"
            and isinstance(func.value, ast.Name)
            and self._is_alias(func.value.id, "pydantic_settings_mod")
        )

    def _pydantic_field_info(
        self, node: ast.AnnAssign
    ) -> Optional[_PydanticField]:
        if not isinstance(node.target, ast.Name):
            return None
        name = node.target.id
        if (
            name == "model_config"
            or name.startswith("_")
            or _is_classvar_annotation(node.annotation)
        ):
            return None

        field_calls = _annotated_metadata(node.annotation)
        field_calls = [
            call for call in field_calls if self._is_pydantic_field_call(call)
        ]
        value_field = (
            node.value
            if node.value is not None
            and self._is_pydantic_field_call(node.value)
            else None
        )
        if isinstance(value_field, ast.Call):
            field_calls.append(value_field)

        alias_node: Optional[ast.AST] = None
        for call in field_calls:
            validation_alias = _keyword_value(call, "validation_alias")
            alias = _keyword_value(call, "alias")
            if not _is_none_literal(validation_alias):
                if validation_alias is not None:
                    alias_node = validation_alias
            elif not _is_none_literal(alias):
                if alias is not None:
                    alias_node = alias

        if alias_node is None:
            alias_names: Optional[Tuple[str, ...]] = ()
            raw_alias = None
        else:
            alias_names = self._pydantic_alias_names(alias_node)
            raw_alias = None if alias_names is not None else _unparse(alias_node)

        has_default, default_node = self._pydantic_field_default(
            node.value, field_calls
        )
        return _PydanticField(
            node=node,
            field_name=name,
            alias_names=alias_names,
            raw_alias=raw_alias,
            has_default=has_default,
            default_node=default_node,
        )

    def _is_pydantic_field_call(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Name):
            return self._is_alias(func.id, "pydantic_field")
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "Field"
            and isinstance(func.value, ast.Name)
            and self._is_alias(func.value.id, "pydantic_mod")
        )

    def _pydantic_alias_names(
        self, node: ast.AST
    ) -> Optional[Tuple[str, ...]]:
        name = _extract_string(node)
        if name is not None:
            return (name,)
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        is_choices = (
            isinstance(func, ast.Name)
            and self._is_alias(func.id, "pydantic_alias_choices")
        ) or (
            isinstance(func, ast.Attribute)
            and func.attr == "AliasChoices"
            and isinstance(func.value, ast.Name)
            and self._is_alias(func.value.id, "pydantic_mod")
        )
        if not is_choices or node.keywords or not node.args:
            return None
        names = tuple(_extract_string(arg) for arg in node.args)
        if any(name is None for name in names):
            return None
        return tuple(name for name in names if name is not None)

    @staticmethod
    def _pydantic_field_default(
        value: Optional[ast.AST],
        field_calls: List[ast.Call],
    ) -> Tuple[bool, Optional[ast.AST]]:
        if value is not None and not (
            isinstance(value, ast.Call) and value in field_calls
        ):
            if _is_ellipsis_literal(value):
                return False, None
            return True, value

        for call in reversed(field_calls):
            default = call.args[0] if call.args else _keyword_value(call, "default")
            if default is not None:
                if _is_ellipsis_literal(default):
                    return False, None
                return True, default
            factory = _keyword_value(call, "default_factory")
            if factory is not None and not _is_none_literal(factory):
                return True, None
        return False, None

    def _record_pydantic_fields(
        self,
        fields: List[_PydanticField],
        config: _PydanticSettingsConfig,
    ) -> None:
        for field_info in fields:
            raw_expr: Optional[str] = None
            if field_info.alias_names is None:
                name = None
                accepted_names: Tuple[str, ...] = ()
                raw_expr = field_info.raw_alias
            elif not field_info.alias_names and config.aliases_dynamic:
                name = None
                accepted_names = ()
                if (
                    config.prefix is None
                    or config.prefix_target is None
                    or config.case_sensitive is None
                ):
                    raw_expr = "model_config"
                else:
                    raw_expr = f"alias_generator({field_info.field_name!r})"
            else:
                is_alias = bool(field_info.alias_names)
                base_names = (
                    field_info.alias_names
                    if is_alias else (field_info.field_name,)
                )
                target = config.prefix_target
                if target is None and config.prefix != "":
                    name = None
                    accepted_names = ()
                    raw_expr = "model_config['env_prefix_target']"
                else:
                    apply_prefix = (
                        target in {"alias", "all"}
                        if is_alias
                        else target in {"variable", "all"}
                    )
                    if apply_prefix and config.prefix is None:
                        name = None
                        accepted_names = ()
                        raw_expr = (
                            "model_config['env_prefix'] + "
                            f"{base_names[0]!r}"
                        )
                    else:
                        prefix = config.prefix if apply_prefix else ""
                        accepted_names = tuple(
                            f"{prefix or ''}{base_name}"
                            for base_name in base_names
                        )
                        name = accepted_names[0]

            if config.case_sensitive is None:
                name = None
                accepted_names = ()
                raw_expr = raw_expr or "model_config['case_sensitive']"

            self._append_usage(
                field_info.node,
                EnvUsage(
                    name=name,
                    file=self.file_path,
                    line=field_info.node.lineno,
                    has_default=field_info.has_default,
                    call_type="pydantic_settings",
                    raw_expr=raw_expr,
                    default_node=field_info.default_node,
                    accepted_names=accepted_names,
                    case_sensitive=config.case_sensitive,
                ),
            )

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
            elif alias.name == "pydantic_settings":
                self._bind(bound, "pydantic_settings_mod")
            elif alias.name == "pydantic":
                self._bind(bound, "pydantic_mod")
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
            if node.module == "pydantic_settings" and alias.name == "*":
                self._bind("BaseSettings", "pydantic_settings_base")
                self._bind(
                    "SettingsConfigDict", "pydantic_settings_config"
                )
                continue
            if node.module == "pydantic" and alias.name == "*":
                self._bind("Field", "pydantic_field")
                self._bind("AliasChoices", "pydantic_alias_choices")
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
            elif (
                node.module == "pydantic_settings"
                and alias.name == "BaseSettings"
            ):
                kind = "pydantic_settings_base"
            elif (
                node.module == "pydantic_settings"
                and alias.name == "SettingsConfigDict"
            ):
                kind = "pydantic_settings_config"
            elif node.module == "pydantic" and alias.name == "Field":
                kind = "pydantic_field"
            elif node.module == "pydantic" and alias.name == "AliasChoices":
                kind = "pydantic_alias_choices"
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
                if kind == "environ_env":
                    config: Optional[_BindingConfig] = self._env_config(value.id)
                elif kind == "pydantic_settings_base":
                    config = self._pydantic_config(value.id)
                else:
                    config = None
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
            if self._is_alias(owner, "pydantic_settings_mod"):
                if value.attr == "BaseSettings":
                    bindings.append(
                        (
                            "pydantic_settings_base",
                            _PydanticSettingsConfig(),
                        )
                    )
                elif value.attr == "SettingsConfigDict":
                    bindings.append(("pydantic_settings_config", None))
            if self._is_alias(owner, "pydantic_mod"):
                if value.attr == "Field":
                    bindings.append(("pydantic_field", None))
                elif value.attr == "AliasChoices":
                    bindings.append(("pydantic_alias_choices", None))
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
            usage.accepted_names,
            usage.case_sensitive,
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


def _literal_string_dict(node: ast.Dict) -> Dict[str, ast.AST]:
    result: Dict[str, ast.AST] = {}
    for key, value in zip(node.keys, node.values):
        name = _extract_string(key)
        if name is not None:
            result[name] = value
    return result


def _dict_has_unpack(node: ast.Dict) -> bool:
    return any(key is None for key in node.keys)


def _extract_prefix_target(node: Optional[ast.AST]) -> Optional[str]:
    target = _extract_string(node)
    if target in {"variable", "alias", "all"}:
        return target
    return None


class _ModelConfigUseFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found = False

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "model_config" and isinstance(node.ctx, ast.Load):
            self.found = True

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_header(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_header(node)

    def _visit_function_header(
        self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        for type_param in getattr(node, "type_params", ()):
            self.visit(type_param)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.args)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for type_param in getattr(node, "type_params", ()):
            self.visit(type_param)


def _mutates_pydantic_model_config(node: ast.AST) -> bool:
    finder = _ModelConfigUseFinder()
    finder.visit(node)
    return finder.found


def _pydantic_field_key(field_info: _PydanticField) -> Tuple[object, ...]:
    return (
        field_info.field_name,
        field_info.node.lineno,
        field_info.alias_names,
        field_info.raw_alias,
        field_info.has_default,
        (
            _unparse(field_info.default_node)
            if field_info.default_node is not None else None
        ),
    )


def _extract_bool(node: Optional[ast.AST]) -> Optional[bool]:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _keyword_value(call: ast.Call, name: str) -> Optional[ast.AST]:
    return next(
        (
            keyword.value
            for keyword in call.keywords
            if keyword.arg == name
        ),
        None,
    )


def _is_none_literal(node: Optional[ast.AST]) -> bool:
    return node is None or (
        isinstance(node, ast.Constant) and node.value is None
    )


def _is_ellipsis_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is Ellipsis


def _annotated_metadata(annotation: ast.AST) -> List[ast.Call]:
    if not isinstance(annotation, ast.Subscript):
        return []
    owner = annotation.value
    if not (
        (isinstance(owner, ast.Name) and owner.id == "Annotated")
        or (isinstance(owner, ast.Attribute) and owner.attr == "Annotated")
    ):
        return []
    value = _subscript_key(annotation)
    if not isinstance(value, ast.Tuple):
        return []
    return [
        item for item in value.elts[1:]
        if isinstance(item, ast.Call)
    ]


def _is_classvar_annotation(annotation: ast.AST) -> bool:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        text = annotation.value.replace(" ", "")
        return text.startswith("ClassVar[") or ".ClassVar[" in text
    if isinstance(annotation, ast.Subscript):
        owner = annotation.value
        if (
            (isinstance(owner, ast.Name) and owner.id == "ClassVar")
            or (isinstance(owner, ast.Attribute) and owner.attr == "ClassVar")
        ):
            return True
        annotated_owner = (
            (isinstance(owner, ast.Name) and owner.id == "Annotated")
            or (isinstance(owner, ast.Attribute) and owner.attr == "Annotated")
        )
        inner = _subscript_key(annotation)
        if annotated_owner and isinstance(inner, ast.Tuple) and inner.elts:
            return _is_classvar_annotation(inner.elts[0])
    return False


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
MAX_SCAN_FILES = 25_000
MAX_SCAN_USAGES = 50_000


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
                if len(files) >= MAX_SCAN_FILES:
                    raise ScanError(
                        f"scan contains more than {MAX_SCAN_FILES} files"
                    )
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

    if len(scan_files) > MAX_SCAN_FILES:
        result.errors.append((
            root,
            f"scan contains more than {MAX_SCAN_FILES} files",
        ))
        return result

    result.scanned_files = scan_files
    for path in scan_files:
        try:
            file_usages = scan_file(path)
        except ScanError as exc:
            result.errors.append((path, str(exc)))
        else:
            remaining = MAX_SCAN_USAGES - len(result.usages)
            if len(file_usages) > remaining:
                if remaining > 0:
                    result.usages.extend(file_usages[:remaining])
                result.errors.append((
                    path,
                    f"scan found more than {MAX_SCAN_USAGES} "
                    "environment-variable usages",
                ))
                if on_file is not None:
                    on_file(path)
                break
            result.usages.extend(file_usages)
        if on_file is not None:
            on_file(path)
    return result
