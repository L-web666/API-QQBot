"""多文件插件的辅助模块 - 可以被入口 __init__.py 通过 import 引用"""


def add(expr: str):
    """计算简单的 加减乘除 表达式，如 '1+2'、'3*4'"""
    expr = expr.replace('×', '*').replace('÷', '/').replace('x', '*').replace('X', '*')
    # 只允许数字和运算符，防止任意代码执行
    allowed = set('0123456789+-*/(). ')
    if not all(c in allowed for c in expr):
        raise ValueError("只支持数字和 + - * / ( )")
    return eval(expr, {"__builtins__": {}}, {})


def greet(name: str) -> str:
    return f"你好 {name}！这是多文件插件（helper.py 提供的功能）～"
