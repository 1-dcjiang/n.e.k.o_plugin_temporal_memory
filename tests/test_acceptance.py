"""三条验收：锚点产出率、删点后链完整、注入体积。"""

from __future__ import annotations

import random

import pytest
from temporal_memory.engine import MemoryEngine

MUNDANE = [
    '今天天气不错', '好', '谢谢', '在吗', '晚安', '收到', '嗯嗯',
    '中午吃了面', '在写作业', '刚下课', '宿舍好热', '在听歌',
]
SIGNALS = [
    '记住，我的学号是 2614401064',
    '我特别焦虑，体测 1000 米怕过不了',
    '第一次配置苏大 VPN，域名邮箱终于能用了',
    '决定了，这学期不选机械设计基础',
    '帮我排查一下 Docker 构建报错',
    '记住，我对猫毛过敏',
]


@pytest.fixture()
def engine(tmp_path):
    return MemoryEngine(str(tmp_path / 'memory.db'), {'anchor_threshold': 0.3})


def test_anchor_rate_over_100_turns(engine):
    """100 轮日常对话产出 3–8 个锚点。"""
    turns = []
    for i in range(100):
        text = SIGNALS[i % len(SIGNALS)] if i % 20 == 0 else MUNDANE[i % len(MUNDANE)]
        turns.append(text)
        engine.observe(text, '嗯，我记下了', ts=1_000_000 + i * 60)

    anchors = engine.store.stats()['anchors']
    assert 3 <= anchors <= 8, f'锚点数 {anchors} 超出 3–8'


def test_chain_survives_random_deletes(engine):
    """随机删 20 个锚点后，链表无断点、条数对得上。"""
    for i in range(60):
        engine.remember(f'第 {i} 件事：体测、复习、选课', ts=1_000_000 + i)

    ids = [a['id'] for a in engine.store.all_anchors()]
    victims = random.Random(20261120).sample(ids, 20)
    for anchor_id in victims:
        assert engine.store.delete_anchor(anchor_id) is True

    integrity = engine.store.chain_integrity()
    assert integrity['ok'] is True, integrity
    assert len(engine.store.iter_chain()) == 40
    assert len(engine.store.iter_chain(ascending=False)) == 40


def test_recall_size_is_under_baseline(engine):
    """50 个查询，召回体积受预算约束，且确实带出锚点而不是只有工作记忆。"""
    corpus = []
    for i in range(40):
        user_text = (
            f'第 {i} 轮：记住，我在苏州大学纳米学院上课，今天讨论了机械设计和化学实验，'
            f'还聊到体测 1000 米配速，以及全飞秒术后要注意用眼休息，别揉眼睛。')
        assistant_text = '明白，我把这些记下来了，术后用眼和体测配速我都会留意。'
        corpus.append(user_text + assistant_text)
        engine.observe(user_text, assistant_text, ts=1_000_000 + i * 3600)

    baseline = sum(len(text) for text in corpus)
    assert baseline > 0
    assert engine.store.stats()['anchors'] >= 10

    sizes = []
    anchored_hits = 0
    for i in range(50):
        result = engine.recall(f'第 {i} 轮 体测 机械设计')
        sizes.append(result['chars'])
        if '## 时间线锚点' in result['prompt_block']:
            anchored_hits += 1
    sizes.sort()
    p95 = sizes[int(len(sizes) * 0.95) - 1]
    assert anchored_hits >= 10, f'只有 {anchored_hits} 次召回带出锚点，检索没真正生效'
    assert p95 <= baseline * 0.35, f'P95 {p95} 超过基线 {baseline} 的 35%'


def test_working_memory_stays_bounded(engine):
    for i in range(30):
        engine.observe(f'第 {i} 句闲聊内容', ts=1_000_000 + i)
    assert engine.store.stats()['working_turns'] <= 8
