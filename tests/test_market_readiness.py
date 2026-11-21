"""上架审查（A1348）三项整改的回归：maintenance 容错、面板 i18n、描述声明。"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from temporal_memory import TemporalMemoryPlugin

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / 'ui' / 'settings.tsx'
LOCALES = {'zh-CN': ROOT / 'i18n' / 'zh-CN.json', 'en': ROOT / 'i18n' / 'en.json'}
CJK = re.compile(r'[\u4e00-\u9fff]')


class FakeConfig:
    def __init__(self):
        self.data = {}

    async def dump(self, timeout: float | None = None):
        return dict(self.data)

    async def update(self, payload, timeout: float | None = None):
        self.data.update(payload)
        return dict(self.data)


def _make_plugin(tmp_path):
    plugin = TemporalMemoryPlugin(ctx=None)
    plugin.storage_dir = str(tmp_path)
    plugin.config = FakeConfig()
    return plugin


# ---------------------------------------------------------------- maintenance 容错


def _two_engines(tmp_path):
    """一个好库 + 一个维护必炸的坏库。"""
    plugin = _make_plugin(tmp_path)
    good = asyncio.run(plugin._ensure_engine('好库'))
    bad = asyncio.run(plugin._ensure_engine('坏库'))

    def boom(*_args, **_kwargs):
        raise RuntimeError('数据库被锁')

    bad.maintenance = boom
    return plugin, good, bad


def test_startup_survives_broken_engine(tmp_path):
    """单个库维护失败不能让启动失败，其他库照常维护。"""
    plugin, good, bad = _two_engines(tmp_path)
    calls = {'good': 0}
    real = good.maintenance

    def counting():
        calls['good'] += 1
        return real()

    good.maintenance = counting
    result = asyncio.run(plugin.on_startup())
    assert result.ok is True
    assert result.value['status'] == 'ready'
    assert calls['good'] == 1
    assert plugin._ready is True
    assert '数据库被锁' in plugin._maintenance_errors['坏库']


def test_timer_survives_broken_engine(tmp_path):
    """定时维护遇到坏库只跳过它，返回值仍然正常，别的库继续维护。"""
    plugin, good, bad = _two_engines(tmp_path)
    result = asyncio.run(plugin.on_maintenance_timer())
    assert result.ok is True
    assert result.value['slept'] == {'anchors': 0, 'semantics': 0}
    assert plugin._maintenance_errors['坏库'] == '数据库被锁'
    assert '好库' not in plugin._maintenance_errors


def test_maintenance_error_clears_after_recovery(tmp_path):
    """库修好之后再跑一次成功维护，错误记录要擦掉。"""
    plugin, good, bad = _two_engines(tmp_path)
    asyncio.run(plugin.on_maintenance_timer())
    assert '坏库' in plugin._maintenance_errors
    del bad.maintenance
    asyncio.run(plugin.on_maintenance_timer())
    assert plugin._maintenance_errors == {}


# ---------------------------------------------------------------- i18n


def _load_locales():
    return {name: json.loads(path.read_text(encoding='utf-8')) for name, path in LOCALES.items()}


def test_locales_have_identical_key_sets():
    bundles = _load_locales()
    keys = {name: set(bundle) for name, bundle in bundles.items()}
    assert keys['zh-CN'] == keys['en'], (
        f"only zh: {sorted(keys['zh-CN'] - keys['en'])}; only en: {sorted(keys['en'] - keys['zh-CN'])}"
    )
    assert len(keys['zh-CN']) >= 180
    for name, bundle in bundles.items():
        empty = [key for key, value in bundle.items() if not str(value).strip()]
        assert not empty, f'{name} has empty values: {empty}'


def test_panel_keys_all_resolve_and_are_all_used():
    """面板里引用的每个 key 都要有译文，反过来也不许留死键。"""
    bundles = _load_locales()
    source = PANEL.read_text(encoding='utf-8')
    used = set(re.findall(r'"(ui\.[A-Za-z0-9_.]+)"', source))
    assert used, '面板没有引用任何 ui.* key，i18n 没接上'
    for name, bundle in bundles.items():
        missing = sorted(key for key in used if key not in bundle)
        assert not missing, f'{name} 缺少面板引用的 key: {missing}'
        dead = sorted(key for key in bundle if key.startswith('ui.') and key not in used)
        assert not dead, f'{name} 有面板没用到的 ui key: {dead}'


def test_panel_has_no_hardcoded_chinese():
    """面板文案必须走 i18n；只剩注释可以是中文。"""
    offenders = [
        f'{index}: {line.strip()}'
        for index, line in enumerate(PANEL.read_text(encoding='utf-8').splitlines(), start=1)
        if CJK.search(line) and not line.lstrip().startswith('//')
    ]
    assert not offenders, '面板里还有硬编码中文: ' + '; '.join(offenders)


@pytest.mark.parametrize('key', [
    'plugin.description',
    'plugin.short_description',
    'entry.observe.description',
])
def test_description_states_model_dependency(key):
    """市场描述要把「靠模型调用、覆盖不保证、明文落本地」写清楚。"""
    bundles = _load_locales()
    zh = bundles['zh-CN'][key]
    en = bundles['en'][key]
    if key == 'entry.observe.description':
        assert '主动调用' in zh
        assert 'call' in en.lower()
        return
    assert 'observe' in zh and 'observe' in en
    if key == 'plugin.description':
        for token in ('覆盖率不保证', '明文', '模型'):
            assert token in zh, token


def test_readme_states_usage_caveats():
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    assert '## 使用前提（必读）' in readme
    for token in ('覆盖率不保证', '明文', 'observe'):
        assert token in readme, token
    for token in ('模型主动调用', '原话证据由模型提交', '对话明文落本地'):
        assert token in readme, token
    assert '## 对记忆的影响（上架说明）' in readme
    for token in ('会写什么', '不会做什么', '删除做到哪一步', '覆盖与准确性'):
        assert token in readme, token


def test_plugin_toml_states_memory_impact():
    text = (ROOT / 'plugin.toml').read_text(encoding='utf-8')
    description = text.split('description = "', 1)[1].split('"', 1)[0]
    short = text.split('short_description = "', 1)[1].split('"', 1)[0]
    assert description.startswith('对记忆的影响：')
    for token in ('明文', '删除', '不自动注入', '覆盖率不保证'):
        assert token in description, token
    for token in ('本机记忆', '明文', '删除'):
        assert token in short, token


def test_plugin_toml_description_states_usage_caveats():
    text = (ROOT / 'plugin.toml').read_text(encoding='utf-8')
    description = text.split('description = "', 1)[1].split('"', 1)[0]
    assert 'observe' in description
    assert '覆盖率不保证' in description
    assert '明文' in description
    short = text.split('short_description = "', 1)[1].split('"', 1)[0]
    assert 'observe' in short
