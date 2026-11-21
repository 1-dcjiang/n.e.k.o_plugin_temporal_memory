"""打分：平淡归零、显式热点、强情绪加权、摘要截断。"""

from __future__ import annotations

import pytest
from temporal_memory import scoring


@pytest.mark.parametrize('text', ['你好', '好的', '谢谢', '嗯', '晚安~'])
def test_mundane_scores_zero(text):
    result = scoring.score_turn(text)
    assert result.score == 0.0
    assert result.is_mundane is True
    assert result.should_anchor is False


def test_empty_input_is_mundane():
    assert scoring.is_mundane('   ') is True


def test_explicit_request_anchors_hot():
    result = scoring.score_turn('记住，我下周三要交机械设计的图纸，别忘了')
    assert result.score >= 0.6
    assert 'explicit' in result.signals
    assert scoring.salience_for(result.emotion, 'explicit' in result.signals) == 'hot'


def test_strong_emotion_gets_bonus():
    plain = scoring.score_turn('我有点紧张，怕体测')
    strong = scoring.score_turn('我特别焦虑，感觉要崩溃了')
    assert strong.emotion >= 0.9
    assert strong.score > plain.score


def test_decision_signal_mid_weight():
    result = scoring.score_turn('我决定了，这学期不选机械设计基础')
    assert 'decision' in result.signals
    assert 0.1 < result.score < 0.6


def test_summarize_truncates_at_sentence_end():
    text = '第一句话说完。' * 12
    summary = scoring.summarize(text, max_chars=40)
    assert len(summary) <= 41
    assert summary.endswith('…')
    assert '。' in summary


def test_extract_semantics_keeps_statements_only():
    text = '苏大邮箱是域名邮箱，需要先连 VPN。今天好累啊。'
    items = scoring.extract_semantics(text)
    assert any('域名邮箱' in item for item in items)
    assert all(len(item) >= 8 for item in items)


def test_time_label_contains_date():
    label = scoring.time_label(1760000000, ['体测', '考试'])
    assert '2025' in label or '2026' in label
    assert '体测' in label
