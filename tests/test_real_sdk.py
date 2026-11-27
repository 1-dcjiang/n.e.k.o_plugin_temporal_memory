"""真实 SDK 契约测试：用宿主同款 collect_entries 验证定时器与入口真的注册上了。

conftest 注入的是假 SDK，跑不出「宿主跳过下划线方法」这类问题；这里另起进程
加载真实 `plugin` 包，只做元数据收集，不启动插件。没有源码树时自动跳过，
可用环境变量 `NEKO_REPO` 指向 N.E.K.O 仓库根目录。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MARKER = Path('plugin') / 'sdk' / 'plugin'


def _detect_repo() -> Path | None:
    env = os.environ.get('NEKO_REPO')
    candidates: list[Path] = [Path(env)] if env else []
    for base in (Path.cwd(), ROOT, *ROOT.parents):
        candidates.append(base)
    seen: set[Path] = set()
    for base in candidates:
        for parent in [base, *base.parents][:4]:
            if parent in seen:
                continue
            seen.add(parent)
            if (parent / MARKER).is_dir():
                return parent
    return None


REPO = _detect_repo()
HAS_SDK = REPO is not None

SCRIPT = r"""
import importlib.util
import os
import sys
from pathlib import Path

root = Path(os.environ["TM_PLUGIN_ROOT"])
spec = importlib.util.spec_from_file_location(
    "temporal_memory", root / "__init__.py",
    submodule_search_locations=[str(root)],
)
m = importlib.util.module_from_spec(spec)
sys.modules["temporal_memory"] = m
assert spec.loader is not None
spec.loader.exec_module(m)
from plugin.sdk.plugin.llm_tool import collect_llm_tool_methods

plugin = m.TemporalMemoryPlugin.__new__(m.TemporalMemoryPlugin)
plugin._routers = []
plugin._dynamic_entries = {}
entries = plugin.collect_entries()
for entry_id, handler in entries.items():
    print(f"{entry_id}\t{getattr(handler.meta, 'event_type', '')}")
for meta, _handler in collect_llm_tool_methods(plugin):
    print(f"LLMTOOL\t{meta.name}")
"""


@pytest.mark.skipif(not HAS_SDK, reason='没有 N.E.K.O 源码树，设置 NEKO_REPO 后重跑')
def test_real_sdk_collects_timer_and_entries():
    env = {**os.environ, 'TM_PLUGIN_ROOT': str(ROOT),
           'PYTHONPATH': str(ROOT.parent)}
    venv = (REPO / '.venv' / 'Scripts' / 'python.exe'
            if os.name == 'nt' else REPO / '.venv' / 'bin' / 'python')
    python = str(venv) if venv.exists() else sys.executable
    result = subprocess.run(
        [python, '-c', SCRIPT],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    lines = [line.split('\t') for line in result.stdout.strip().splitlines() if '\t' in line]
    found = {row[0]: row[1] for row in lines if row[0] != 'LLMTOOL'}
    tools = {row[1] for row in lines if row[0] == 'LLMTOOL'}
    assert found.get('maintenance') == 'timer', found
    assert found.get('update_settings') == 'plugin_entry', found
    assert 'observe' in found and 'recall' in found
    assert 'recall_temporal_memory' in tools, tools
    assert found.get('resolve_conflict') == 'plugin_entry', found
    assert found.get('undo_last') == 'plugin_entry', found
    assert found.get('export_memories') == 'plugin_entry', found
    assert found.get('purge_memories') == 'plugin_entry', found
    assert 'retier' not in found
