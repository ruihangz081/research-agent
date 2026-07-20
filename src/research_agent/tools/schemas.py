"""从 Python 函数签名自动生成 OpenAI function calling JSON schema。"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Literal, get_type_hints

# Python type → JSON schema type
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _python_type_to_json(tp: Any) -> dict[str, Any]:
    """将 Python 类型转为 JSON schema 类型描述。"""
    # 基础类型
    if tp in _TYPE_MAP:
        return {"type": _TYPE_MAP[tp]}

    # list[X]
    origin = getattr(tp, "__origin__", None)
    if origin is Literal:
        values = list(getattr(tp, "__args__", ()))
        value_type = type(values[0]) if values else str
        return {"type": _TYPE_MAP.get(value_type, "string"), "enum": values}
    if origin is list:
        args = getattr(tp, "__args__", ())
        item_schema = _python_type_to_json(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item_schema}

    # dict[str, X]
    if origin is dict:
        return {"type": "object"}

    # 兜底
    return {"type": "string"}


def _is_optional(tp: Any) -> tuple[bool, Any]:
    """检查是否为 Optional[X]，返回 (is_optional, inner_type)。"""
    origin = getattr(tp, "__origin__", None)
    # typing.Union
    if origin is not None:
        import typing
        if hasattr(typing, "UnionType"):
            # Python 3.10+ X | None
            pass
        args = getattr(tp, "__args__", ())
        if type(None) in args:
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                return True, non_none[0]
    return False, tp


def generate_schema(
    func: Callable,
    name: str | None = None,
    description: str = "",
) -> dict[str, Any]:
    """生成 OpenAI function calling 的 JSON schema。

    参数
    ----
    func: 要生成 schema 的函数
    name: 工具名（默认取 func.__name__）
    description: 工具描述（默认取 docstring 第一行）
    """
    tool_name = name or func.__name__
    tool_desc = description or (func.__doc__ or "").strip().split("\n")[0]

    sig = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    # 解析参数的 docstring 描述（简易版：从 Args: 段落提取）
    param_docs = _parse_param_docs(func.__doc__ or "")

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue

        tp = hints.get(param_name, str)

        # 检查 Optional
        is_opt, inner_tp = _is_optional(tp)
        # 有默认值也视为可选
        has_default = param.default is not inspect.Parameter.empty

        prop = _python_type_to_json(inner_tp)
        if param_name in param_docs:
            prop["description"] = param_docs[param_name]

        properties[param_name] = prop

        if not is_opt and not has_default:
            required.append(param_name)

    schema: dict[str, Any] = {
        "name": tool_name,
        "description": tool_desc,
        "parameters": {
            "type": "object",
            "properties": properties,
        },
    }
    if required:
        schema["parameters"]["required"] = required

    return schema


def _parse_param_docs(docstring: str) -> dict[str, str]:
    """从 docstring 的 Args 段落提取参数描述（简易版）。"""
    result: dict[str, str] = {}
    in_args = False
    for line in docstring.split("\n"):
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            in_args = True
            continue
        if in_args:
            if stripped == "" or (not stripped.startswith(" ") and ":" not in stripped):
                # 离开 Args 段落
                if stripped and not stripped[0].isspace():
                    in_args = False
                continue
            if ":" in stripped:
                parts = stripped.split(":", 1)
                param_name = parts[0].strip()
                param_desc = parts[1].strip()
                result[param_name] = param_desc
    return result
