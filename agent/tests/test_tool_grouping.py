from types import SimpleNamespace

import pytest

from rimagent.context import Context
from rimagent.llm import LLM, LLMReply
from rimagent.loop import _group_bridge_tools, think
from rimagent.registry import Registry
from rimagent.tools import meta
from rimagent.usage import LLMBlocked


def context(clean_brain):
    registry = Registry()
    registry.add_module(meta)
    registry.add_bridge_methods([{'method': f'ui.method_with_underscore_{i}', 'doc': f'test operation {i}'} for i in range(150)])
    return Context(bridge=SimpleNamespace(), registry=registry, llm=SimpleNamespace(provider=SimpleNamespace(max_tools=128)), config={'play': {'max_tool_calls': 3}})


def test_grouping_keeps_all_methods_and_exact_bindings(clean_brain):
    ctx = context(clean_brain)
    specs = ctx.registry.specs()
    grouped, catalog, routes = _group_bridge_tools(ctx, specs)
    assert len(grouped) <= 128 and len(routes) == 150
    assert routes['ui.method_with_underscore_149'] == 'rw_ui_method_with_underscore_149'
    assert 'test operation 149' in catalog
    assert any(t['function']['name'] == 'end_turn' for t in grouped)
    filtered = [s for s in specs if s['function']['name'] != 'rw_ui_method_with_underscore_149']
    assert 'ui.method_with_underscore_149' not in _group_bridge_tools(ctx, filtered)[2]
    ctx.llm.provider = SimpleNamespace()  # Qwen offers every original function
    assert _group_bridge_tools(ctx, specs) == (specs, '', {})


@pytest.mark.parametrize('method,expected_calls', [('ui.method_with_underscore_149', 1), ('dev.not_advertised', 0)])
def test_grouped_call_uses_registry_and_preserves_argument_coercion(clean_brain, method, expected_calls):
    ctx = context(clean_brain)
    calls = []
    ctx.bridge = SimpleNamespace(call=lambda method, **params: calls.append((method, params)) or {'ok': True})
    class Scripted:
        provider = SimpleNamespace(max_tools=128)
        n = 0
        def chat(self, messages, tools, **kw):
            assert len(tools) <= 128
            self.n += 1
            if self.n == 1:
                assert 'Bridge tool catalog' in messages[0]['content']
                return LLMReply(tool_calls=[{'id': 'call1', 'name': 'rimagent_bridge_call', 'arguments': {'method': method, 'params': {'at': '[1,2]'}}}])
            return LLMReply(tool_calls=[{'id': 'call2', 'name': 'end_turn', 'arguments': {'notes': 'done'}}])
        def assistant_message(self, reply):
            return LLM.assistant_message(self, reply)
    ctx.llm = Scripted()
    result = think(ctx, 'test', system='test')
    assert result.calls == 2 and len(calls) == expected_calls
    if calls:
        assert calls[0] == (method, {'at': [1, 2]})


def test_non_bridge_overflow_fails_before_request(clean_brain):
    ctx = context(clean_brain)
    for t in ctx.registry.tools.values():
        t.source = 'brain'
    with pytest.raises(LLMBlocked, match='tool_limit'):
        _group_bridge_tools(ctx, ctx.registry.specs())
