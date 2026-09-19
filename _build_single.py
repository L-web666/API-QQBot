# -*- coding: utf-8 -*-
"""把多模块版本打包成单文件 qqbot_single.py（保留注释与 docstring，合并 import）"""
import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# 模块顺序（与单文件内的依赖顺序一致）
MODULES = [
    ('deferred_writes', 'core/deferred_writes.py'),
    ('config_manager', 'core/config_manager.py'),
    ('logger', 'core/logger.py'),
    ('context_manager', 'core/context_manager.py'),
    ('qq_client', 'core/qq_client.py'),
    ('ai_client', 'core/ai_client.py'),
    ('message_filter', 'core/message_filter.py'),
    ('file_handler', 'core/file_handler.py'),
    ('stats', 'core/stats.py'),
    ('plugin_manager', 'core/plugin_manager.py'),
    ('message_processor', 'core/message_processor.py'),
    ('web_admin', 'core/web_admin.py'),
    ('cloud_sync', 'core/cloud_sync.py'),
]

IMPORT_RE = re.compile(r'^(import |from )')


def extract_top_imports(src: str):
    """用 AST 提取顶层 import 语句原文（去重，保持出现顺序）"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines()
    out = []
    seen = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # 跳过 from core.xxx import ...（单文件内不需要）
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('core'):
                continue
            start = node.lineno - 1
            end = node.end_lineno if node.end_lineno else start + 1
            text = '\n'.join(lines[start:end]).strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return out


def strip_module(src: str) -> str:
    """移除文件级 docstring 与顶层 import 行，保留其余（含注释、函数体内的局部 import）"""
    lines = src.splitlines(keepends=True)
    # 收集要删除的行号集合（1-based）
    drop = set()
    try:
        tree = ast.parse(src)
        # 文件级 docstring：开头连续的字符串表达式
        first = tree.body[0] if tree.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            for ln in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                drop.add(ln)
        # 顶层 import（含 from core.xxx import ...）
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    drop.add(ln)
    except SyntaxError:
        pass
    result = []
    for i, line in enumerate(lines, start=1):
        if i in drop:
            continue
        result.append(line)
    # 去掉模块末尾多余空行
    while result and not result[-1].strip():
        result.pop()
    return ''.join(result).rstrip() + '\n'


def main():
    imports = []
    bodies = []
    for name, path in MODULES:
        with io.open(os.path.join(ROOT, path), encoding='utf-8') as f:
            src = f.read()
        for imp in extract_top_imports(src):
            if imp not in imports:
                imports.append(imp)
        bodies.append((name, strip_module(src)))

    # 主程序
    with io.open(os.path.join(ROOT, 'qqbot.py'), encoding='utf-8') as f:
        main_src = f.read()
    for imp in extract_top_imports(main_src):
        if imp not in imports:
            imports.append(imp)
    main_body = strip_module(main_src)
    bodies.append(('qqbot', main_body))

    out = io.StringIO()
    out.write('#!/usr/bin/env python3\n')
    out.write('"""\n')
    out.write('QQ AI Bot - 单文件整合版 (v1.4.0)\n')
    out.write('===================================\n')
    out.write('由以下模块合并生成（保留原始注释，代码与多模块版本完全一致）：\n')
    out.write('config_manager / logger / context_manager / qq_client / ai_client /\n')
    out.write('message_filter / file_handler / message_processor / web_admin / qqbot(主程序)\n')
    out.write('\n')
    out.write('用法：\n')
    out.write('    python qqbot_single.py\n')
    out.write('\n')
    out.write('依赖：\n')
    out.write('    pip install -r requirements.txt   (requests、websocket-client)\n')
    out.write('\n')
    out.write('运行说明：\n')
    out.write('    - config.json 需与本文件放在同一目录（不存在会自动创建模板）\n')
    out.write('    - 日志与用户上下文生成在 data/ 目录下\n')
    out.write('    - 可用 start.bat / start.sh 启动（崩溃自动重启）\n')
    out.write('"""\n')
    out.write('\n')
    out.write('# ======================== 依赖导入 ========================\n')
    for imp in imports:
        out.write(imp + '\n')
    out.write('\n')
    for name, body in bodies:
        out.write(f'# ======================== 模块: {name} ========================\n')
        out.write(body)
        out.write('\n')

    with io.open(os.path.join(ROOT, 'qqbot_single.py'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(out.getvalue())
    print(f"已生成 qqbot_single.py（{len(imports)} 条 import，{len(bodies)} 个模块）")


if __name__ == '__main__':
    main()
