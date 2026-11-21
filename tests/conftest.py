"""测试夹具：注入假 SDK，让插件包可以在没有宿主的情况下导入。

插件入口 __init__.py 依赖 plugin.sdk.plugin，真实 SDK 只存在于 N.E.K.O 进程内，
所以这里在导入包之前先把三个模块塞进 sys.modules。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 包目录本身叫 temporal_memory，所以导入路径要插它的父目录
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


class _SdkError(Exception):
    def __init__(self, message: str = '') -> None:
        self.message = message
        super().__init__(message)


class _Result:
    def __init__(self, value=None, error=None) -> None:
        self.value = value
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None

    def __repr__(self) -> str:
        return f'_Result(ok={self.ok}, value={self.value!r}, error={self.error!r})'


def _decorator_factory(**_kwargs):
    def wrap(func):
        return func

    return wrap


class _Ui:
    action = staticmethod(_decorator_factory)
    context = staticmethod(_decorator_factory)


class _FakePluginBase:
    def __init__(self, ctx=None) -> None:
        self.ctx = ctx
        self.plugin_id = 'temporal_memory'
        self.storage_dir = str(PROJECT_ROOT / 'data')
        self._logger: list[str] = []

    @property
    def logger(self):
        class _Logger:
            def info(self, *_a, **_k):
                return None

            def warning(self, *_a, **_k):
                return None

            def error(self, *_a, **_k):
                return None

        return _Logger()

    def data_path(self, *parts):
        base = Path(self.storage_dir)
        base.mkdir(parents=True, exist_ok=True)
        return base.joinpath(*parts)

    def report_status(self, _payload):
        return None


def _host_plugin_package_dirs() -> list[str]:
    """插件挂进 N.E.K.O 的 plugin/plugins 后，测试会被导入成
    plugin.plugins.temporal_memory.tests.*，需要 plugin.plugins 能解析。
    独立目录（打包前的源码树）找不到宿主 plugin 包，返回空表保持隔离。"""
    for base in Path(__file__).resolve().parents[:4]:
        if (base / 'plugins').is_dir() and (base / 'sdk').is_dir():
            return [str(base)]
    return []


def _install_fake_sdk() -> None:
    if 'plugin.sdk.plugin' in sys.modules:
        return
    sdk = types.ModuleType('plugin.sdk.plugin')
    sdk.NekoPluginBase = _FakePluginBase
    sdk.neko_plugin = lambda cls: cls
    sdk.lifecycle = _decorator_factory
    sdk.plugin_entry = _decorator_factory
    sdk.timer_interval = _decorator_factory
    sdk.llm_tool = _decorator_factory
    sdk.ui = _Ui
    sdk.Ok = lambda value=None: _Result(value=value)
    sdk.Err = lambda error: _Result(error=error)
    sdk.SdkError = _SdkError
    sdk.tr = lambda key, default=None, **_kw: default if default is not None else key

    package = types.ModuleType('plugin')
    # 空表只挡真实 SDK 的子包；挂进宿主时补上真实 plugin 包路径，
    # 否则 plugin.plugins 不可导入，测试在 CI 里全部 collect error。
    package.__path__ = _host_plugin_package_dirs()
    sdk_pkg = types.ModuleType('plugin.sdk')
    sdk_pkg.__path__ = []
    package.sdk = sdk_pkg
    sdk_pkg.plugin = sdk

    sys.modules['plugin'] = package
    sys.modules['plugin.sdk'] = sdk_pkg
    sys.modules['plugin.sdk.plugin'] = sdk


_install_fake_sdk()
