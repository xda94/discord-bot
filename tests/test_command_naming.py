"""Keep the public Discord command surface in kebab-case."""

import ast
from pathlib import Path


FEATURES_DIR = Path(__file__).resolve().parent.parent / "features"


def test_registered_command_and_option_names_are_kebab_case():
    violations = []
    for path in FEATURES_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "command":
                for keyword in node.keywords:
                    if (
                        keyword.arg == "name"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                        and "_" in keyword.value.value
                    ):
                        violations.append(f"{path.name}: /{keyword.value.value}")
            elif node.func.attr == "rename":
                for keyword in node.keywords:
                    if (
                        isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                        and "_" in keyword.value.value
                    ):
                        violations.append(
                            f"{path.name}: option {keyword.value.value}"
                        )

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [
                decorator
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
            ]
            if not any(decorator.func.attr == "command" for decorator in decorators):
                continue
            renamed = {
                keyword.arg
                for decorator in decorators
                if decorator.func.attr == "rename"
                for keyword in decorator.keywords
            }
            # The first callback argument is the Discord interaction itself;
            # every later argument becomes a public slash-command option.
            for argument in node.args.args[1:]:
                if "_" in argument.arg and argument.arg not in renamed:
                    violations.append(
                        f"{path.name}: unrenamed option {argument.arg}"
                    )

    assert violations == []
