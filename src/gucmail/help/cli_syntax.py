"""Inert CLI grammar for checking examples without executing their commands.

This checks tokens, scopes, aliases and arity. It deliberately does not convert
types, inspect files, resolve defaults/environment variables or invoke callbacks.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import typer
    from typer.core import TyperCommand, TyperGroup


@dataclass(frozen=True)
class CliParameterSyntax:
    name: str
    aliases: tuple[str, ...] = ()
    arity: int = 1
    required: bool = False
    exits_early: bool = False


@dataclass(frozen=True)
class CliCommandSyntax:
    name: str
    parameters: tuple[CliParameterSyntax, ...]
    children: tuple[CliCommandSyntax, ...] = ()
    is_group: bool = False
    invoke_without_command: bool = False
    allow_interspersed_args: bool = True
    allow_extra_args: bool = False

    def validate(self, example: str) -> str | None:
        """Return a grammar error, or None; input excludes the executable name."""

        # Backslashes in documented Windows paths are literal, not POSIX escapes.
        # Both quote styles group words; no shell expansion or execution occurs.
        lexer = shlex.shlex(example, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.escape = ""
        try:
            tokens = list(lexer)
        except ValueError:
            return "Unclosed quote in CLI example."
        return self._validate_tokens(tokens)

    def _validate_tokens(self, tokens: list[str]) -> str | None:
        options = {alias: parameter for parameter in self.parameters for alias in parameter.aliases}
        arguments = tuple(parameter for parameter in self.parameters if not parameter.aliases)
        seen: set[str] = set()
        positional: list[str] = []
        early_exit = False
        index = 0
        options_enabled = True
        while index < len(tokens):
            word = tokens[index]
            index += 1
            if options_enabled and word == "--":
                options_enabled = False
                continue
            if options_enabled and word.startswith("-") and word != "-":
                alias, separator, attached = word.partition("=")
                if alias in options:
                    expanded = [(alias, attached if separator else None)]
                elif word.startswith("--"):
                    return f"Unknown option for {self.name or 'gucmail'}: {alias}"
                else:
                    # Short flag bundles, including a value attached to the final option.
                    expanded = []
                    for offset, letter in enumerate(word[1:], start=2):
                        alias = "-" + letter
                        parameter = options.get(alias)
                        if parameter is None:
                            return f"Unknown option for {self.name or 'gucmail'}: {alias}"
                        value = None
                        if parameter.arity:
                            value = word[offset:] or None
                        expanded.append((alias, value))
                        if parameter.arity:
                            break
                for alias, value in expanded:
                    parameter = options[alias]
                    if not parameter.arity and value is not None:
                        return f"Option {alias} does not take a value."
                    needed = parameter.arity - (value is not None)
                    if len(tokens) - index < needed:
                        return f"Option {alias} requires {parameter.arity} value(s)."
                    index += max(0, needed)
                    seen.add(parameter.name)
                    early_exit |= parameter.exits_early
                continue
            positional.append(word)
            if not self.allow_interspersed_args:
                positional.extend(tokens[index:])
                break

        # Eager help/version callbacks bypass missing required parameters and
        # command resolution, but the option parser still rejects malformed options.
        if early_exit:
            return None
        for parameter in self.parameters:
            if parameter.aliases and parameter.required and parameter.name not in seen:
                return f"Missing required option: {parameter.aliases[0]}"
        consumed = 0
        for parameter in arguments:
            available = len(positional) - consumed
            if parameter.arity == -1:
                if parameter.required and available == 0:
                    return f"Missing argument: {parameter.name}"
                consumed = len(positional)
            elif available < parameter.arity:
                if available or parameter.required:
                    return f"Argument {parameter.name} requires {parameter.arity} value(s)."
            else:
                consumed += parameter.arity
        remaining = positional[consumed:]
        if self.is_group:
            if not remaining:
                return None if self.invoke_without_command else "Missing subcommand."
            child = next((child for child in self.children if child.name == remaining[0]), None)
            if child is None:
                return f"Unknown subcommand: {remaining[0]}"
            return child._validate_tokens(remaining[1:])
        if remaining and not self.allow_extra_args:
            return "Unexpected extra argument(s)."
        return None


def syntax_from_command(
    command: TyperCommand | TyperGroup,
    *,
    eager_exit_parameters: frozenset[str] = frozenset(),
    parent: typer.Context | None = None,
    name: str = "",
) -> CliCommandSyntax:
    """Copy grammar from Typer's installed tree, retaining no executable objects."""

    import typer
    from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption

    context = typer.Context(command, parent=parent, **command.context_settings)
    help_option = command.get_help_option(context)
    parameters = tuple(
        CliParameterSyntax(
            name=parameter.name or "",
            aliases=(
                ()
                if isinstance(parameter, TyperArgument)
                else tuple((*parameter.opts, *parameter.secondary_opts))
            ),
            arity=(
                0
                if isinstance(parameter, TyperOption) and (parameter.is_flag or parameter.count)
                else parameter.nargs
            ),
            required=parameter.required,
            exits_early=parameter is help_option or parameter.name in eager_exit_parameters,
        )
        for parameter in command.get_params(context)
    )
    is_group = isinstance(command, TyperGroup)
    children: list[CliCommandSyntax] = []
    if isinstance(command, TyperGroup):
        for child_name, child in command.commands.items():
            if not isinstance(child, (TyperCommand, TyperGroup)):
                raise ValueError("Help grammar requires a Typer command tree.")
            children.append(syntax_from_command(child, parent=context, name=child_name))
    return CliCommandSyntax(
        name=name,
        parameters=parameters,
        children=tuple(children),
        is_group=is_group,
        invoke_without_command=(
            command.invoke_without_command if isinstance(command, TyperGroup) else False
        ),
        allow_interspersed_args=context.allow_interspersed_args,
        allow_extra_args=context.allow_extra_args,
    )
