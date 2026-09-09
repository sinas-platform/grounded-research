"""Every name a function reads must exist by the time it runs.

`from __future__ import annotations` hides unresolvable annotations, and
nothing hides an unresolvable *value*: it raises NameError at the moment the
line executes, which for a batch stage is after the work has been queued.

The case this was written for: `guidance_by_class` was built inside
`_load_shared`, which returned three values and not that one, and read twice
inside `stage_extract`. Every test passed, because no test runs `stage_extract`.
A bulk extraction would have raised on its first document.

This is the runtime-name counterpart to the annotation sweep and to the loader
arity test: three different mechanisms for the same failure, which is a thing
that is wrong and reports nothing until it is too late to be cheap.
"""

from __future__ import annotations

import ast
import builtins
import pathlib

MODULES = ["app/bulk_pipeline.py"]


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _own_nodes(scope: ast.AST):
    """Every node in this scope, stopping at the edge of a nested one.

    `ast.walk` crosses scope boundaries, which makes a nested function's
    parameters look like names its parent reads and never binds. Each scope has
    to be read on its own, with the enclosing chain supplying the rest.
    """
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _bound_names(scope: ast.AST) -> set[str]:
    """Names this scope binds: parameters, assignments, imports, comprehension
    targets, except-handlers, and the names of things defined inside it."""
    out: set[str] = set()
    args = getattr(scope, "args", None)
    if args is not None:
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            out.add(a.arg)
        for a in (args.vararg, args.kwarg):
            if a is not None:
                out.add(a.arg)
    for node in _own_nodes(scope):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        elif isinstance(node, _SCOPES) and hasattr(node, "name"):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                out.add((al.asname or al.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            out.update(node.names)
    return out


def _read_names(scope: ast.AST) -> set[str]:
    return {
        n.id for n in _own_nodes(scope)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def test_every_name_a_function_reads_is_one_it_can_reach():
    root = pathlib.Path(__file__).resolve().parents[1]
    unresolved: list[str] = []
    for rel in MODULES:
        path = root / rel
        tree = ast.parse(path.read_text())
        base = _bound_names(tree) | set(dir(builtins))

        def walk(node: ast.AST, visible: set[str]) -> None:
            """Depth-first, carrying what each enclosing scope binds.

            A nested function sees its enclosing function's names, so the
            visible set has to accumulate down the chain or every closure
            reads as unresolved.
            """
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inner = visible | _bound_names(child)
                    missing = _read_names(child) - inner
                    for name in sorted(missing):
                        unresolved.append(f"{rel}::{child.name} reads {name!r}")
                    walk(child, inner)
                elif isinstance(child, ast.ClassDef):
                    walk(child, visible | _bound_names(child))
                else:
                    walk(child, visible)

        walk(tree, base)
    assert not unresolved, "names read but never bound:\n  " + "\n  ".join(unresolved)
