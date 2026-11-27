"""锚点打分与摘要生成：纯规则、零模型调用，保证写入路径足够便宜。

打分只输出四类信号（显式记忆 / 情绪 / 首次知识 / 决策计划），
权重可被配置覆盖；低于阈值就只留在工作记忆里，不占时间线。
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field


def _en_word(word: str) -> str:
    """英文词边界：CJK 也算 `\\w`，`\\b` 会在「这个bug」这类混写里失效。"""
    return rf'(?<![A-Za-z]){word}(?![A-Za-z])'


# 权重按「可信度 × 持续时间」设计：显式要求最重，闲聊里的任务动词最轻
SIGNAL_PATTERNS: tuple[tuple[str, str, float], ...] = (
    ('explicit', rf'记住|记住这件事|别忘|不要忘|很重要|务必|{_en_word("remember")}|{_en_word("do not forget")}', 0.45),
    ('emotion', rf'焦虑|崩溃|难过|生气|害怕|紧张|失望|开心|兴奋|孤独|{_en_word("scared")}|{_en_word("anxious")}|{_en_word("upset")}|{_en_word("angry")}|{_en_word("happy")}|{_en_word("excited")}', 0.30),
    ('knowledge', rf'第一次|首次|刚学会|刚配置|报错|错误|{_en_word("error")}|{_en_word("bug")}|不兼容|文档里说', 0.30),
    ('decision', rf'决定|计划|打算|报名|截止|{_en_word("deadline")}|明天|下周|下个月|选择要', 0.25),
    ('task', r'帮我|排查|修复|提交|申请|处理一下|设定', 0.10),
)

STRONG_EMOTION = re.compile(r'崩溃|绝望|狂喜|极度|特别焦虑|特别开心|panic|devastated')
MILD_EMOTION = re.compile(r'有点|稍微|还好|没什么|轻微')

MUNDANE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'^(你好|您好|早安|晚安|在吗|谢谢|好的|收到|ok|hi|hello|thanks)[啊呀~！!。.？?\s]*$', re.I),
    re.compile(r'^\W*$'),
)

DOMAIN_TERMS: tuple[str, ...] = (
    'github', 'sheerid', 'steam', 'workshop', 'api', 'token', 'key', 'sqlite',
    'docker', 'windows', 'linux', 'python', 'typescript', '内存', '显存', '配置',
    '报错', '学生包', '考试', '复习', '期中', '期末', '论文', '简历', '面试',
    '工坊', '角色卡', '人设', '模型', '插件',
)

_ASCII_TOKEN = re.compile(r'[A-Za-z][A-Za-z0-9_+-]{2,}')


@dataclass
class ScoreResult:
    score: float
    signals: list[str] = field(default_factory=list)
    emotion: float = 0.0
    tags: list[str] = field(default_factory=list)
    is_mundane: bool = False

    @property
    def should_anchor(self) -> bool:
        return not self.is_mundane and self.score > 0

    def as_dict(self) -> dict:
        return {
            'score': round(self.score, 3),
            'signals': list(self.signals),
            'emotion': round(self.emotion, 3),
            'tags': list(self.tags),
            'is_mundane': self.is_mundane,
        }


def _normalize(text: str) -> str:
    return (text or '').strip()


def is_mundane(text: str) -> bool:
    normalized = _normalize(text)
    if not normalized:
        return True
    if len(normalized) <= 6 and not any(p.search(normalized) for p in MUNDANE_PATTERNS):
        # 极短句通常是「嗯」「好」这类，先按平淡处理；显式信号会在打分里翻案
        return True
    return any(p.search(normalized) for p in MUNDANE_PATTERNS)


def emotion_weight(text: str, signals: Iterable[str] = ()) -> float:
    normalized = _normalize(text)
    if STRONG_EMOTION.search(normalized):
        return 0.9
    if MILD_EMOTION.search(normalized):
        return 0.3
    if 'emotion' in set(signals):
        return 0.7
    return 0.0


def extract_tags(text: str, limit: int = 6) -> list[str]:
    """标签只收领域词与 ASCII 词：整句汉字切块会产生大量不稳定标签，把语义网撑稠。"""
    normalized = _normalize(text).lower()
    tags: list[str] = []
    for term in DOMAIN_TERMS:
        if term in normalized:
            tags.append(term)
    for match in _ASCII_TOKEN.findall(normalized):
        tags.append(match)
    seen: set[str] = set()
    ordered = [t for t in tags if not (t in seen or seen.add(t))]
    return ordered[:limit]


def score_turn(user_text: str, assistant_text: str = '',
               *, threshold: float = 0.5) -> ScoreResult:
    """打分只以用户发言为准；助手回复不参与任何信号判定。

    旧实现把 user+assistant 合并匹配：助手一句「别紧张」就能让用户的「好的」
    以一个情绪词 0.3 分的权重建出锚点。
    """
    user = _normalize(user_text)
    signals: list[str] = []
    total = 0.0
    for name, pattern, weight in SIGNAL_PATTERNS:
        if re.search(pattern, user, re.I):
            signals.append(name)
            total += weight

    if is_hypothesis(user):
        # 假设句也要过阈值才落库；否则「假设明天可能下雨」这种闲聊会绕开稀疏策略。
        signals.append('hypothesis')
        total += 0.35

    # 先认信号再判平淡：「明天考试」「报名了」这类短句不能因为长度被吃掉
    mundane = is_mundane(user) and not signals
    if mundane:
        total = 0.0

    emotion = emotion_weight(user, signals)
    if emotion >= 0.9:
        total += 0.15  # 强情绪单独加分，避免被长句摊薄
    return ScoreResult(
        score=min(1.0, total),
        signals=signals,
        emotion=emotion,
        tags=extract_tags(user),
        is_mundane=mundane,
    )


def summarize(user_text: str, assistant_text: str = '', max_chars: int = 140) -> str:
    """锚点摘要只收用户原句；助手回复不是用户证据，不能混进「用户原话」标签。"""
    user_text = _normalize(user_text)
    text = user_text
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    for sep in ('。', '！', '？', '；', '.', ';'):
        index = cut.rfind(sep)
        if index > max_chars * 0.5:
            cut = cut[: index + 1]
            break
    return cut.rstrip() + '…'


def time_label(ts: int, tags: Iterable[str] = ()) -> str:
    """人可读的时间标签，供锚点做「跳轴」的文本线索。"""
    local = time.localtime(ts)
    base = time.strftime('%Y-%m-%d %H:%M', local)
    top = [t for t in tags if t][:2]
    return f'{base} {"·".join(top)}' if top else base


def salience_for(emotion: float, explicit: bool, threshold_hot: float = 0.8) -> str:
    if explicit or emotion >= threshold_hot:
        return 'hot'
    return 'warm'


SEMANTIC_CUES: tuple[str, ...] = (
    '用了', '使用', '支持', '需要', '依赖', '配置', '地址', '端口',
    '版本', '协议', '兼容', '不支持', '报错是因为',
)

PREFERENCE_CUES: tuple[str, ...] = (
    '喜欢', '偏爱', '讨厌', '不喜欢', '习惯', '常用', '爱吃', '过敏',
    '忌口', '口味', '偏好', '受不了', '最烦', '想要', '希望',
)
DECISION_CUES: tuple[str, ...] = (
    '决定', '计划', '打算', '报名', '截止', '约好', '定了', '准备',
)
FACT_CUES: tuple[str, ...] = (
    '住在', '工作在', '我是', '我叫', '我的', '用的是', '版本是',
    '地址是', '端口是', '配置是', '养了', '家里有',
)


HYPOTHESIS_PREFIX_CUES: tuple[str, ...] = (
    '假设', '假如', '倘若', '假装', '角色扮演', '来玩', '玩个',
    '想象一下', '做个假设', '开个玩笑', '开玩笑', '逗你',
)
HYPOTHESIS_FIRST_PERSON: tuple[str, ...] = (
    '如果我是', '如果我变成', '如果我是你', '如果让你', '要是我是',
    '要是让你', '如果我是一只', '如果我变成一只', '如果你是我',
)
# 「如果」「要是」「设定」「测试一下」单独出现不再算假设：
# 前者常是条件规则（记住，如果我说红色就停），后者常是闹钟/日程。
HYPOTHESIS_ROLE_PATTERN = re.compile(
    r'(?:如果|要是).{0,12}(?:你当|你扮演|你变成|你是)')
HYPOTHESIS_SETTING_PATTERN = re.compile(
    r'设定(?:一个|个)?(?:角色|场景|世界|规则|人设|剧情|故事)')
EXPLICIT_MEMORY_PATTERN = re.compile(
    r'记住|记下|别忘|不要忘|务必记住|remember', re.IGNORECASE)
_HYPOTHESIS_ASCII = re.compile(
    r'\b(?:what if|suppose|pretend|role ?play|hypothetically)\b', re.IGNORECASE)


def is_explicit_memory(text: str) -> bool:
    """显式「记住/别忘」优先：这类句子即使带「如果」也必须是事实，不是假设。"""
    return bool(EXPLICIT_MEMORY_PATTERN.search(_normalize(text)[:80]))


def is_hypothesis(text: str) -> bool:
    """识别玩笑、假设、角色扮演设定；宁可漏判，也不把真实偏好打成假设。

    只在句首出现假设词，或出现「假设类动词 + 第一人称/角色」时命中；
    「如果」「要是」「设定」「测试一下」单独出现不再命中，避免把
    「记住，如果我说红色就停」「我对花生过敏，如果做饭注意」「设定闹钟」
    这类真实规则/事实丢进假设区。
    """
    normalized = _normalize(text).strip()
    if not normalized:
        return False
    if is_explicit_memory(normalized):
        return False
    head = normalized[:80].lower()
    if any(head.startswith(cue) for cue in HYPOTHESIS_PREFIX_CUES):
        return True
    if any(cue in head for cue in HYPOTHESIS_FIRST_PERSON):
        return True
    if HYPOTHESIS_ROLE_PATTERN.search(head):
        return True
    if HYPOTHESIS_SETTING_PATTERN.search(head):
        return True
    return bool(_HYPOTHESIS_ASCII.search(head))


def classify_memory(text: str) -> str:
    """把一句原话归成事件/偏好/决定/事实；未知按事件处理。"""
    normalized = _normalize(text).lower()
    if any(cue in normalized for cue in PREFERENCE_CUES):
        return 'preference'
    if any(cue in normalized for cue in DECISION_CUES):
        return 'decision'
    if any(cue in normalized for cue in FACT_CUES):
        return 'fact'
    if any(cue in normalized for cue in SEMANTIC_CUES):
        return 'fact'
    return 'event'


def _bigrams(text: str) -> set[str]:
    text = re.sub(r'[\s，,。.、；;：:！!？?（）()【】\[\]"“”]', '', _normalize(text).lower())
    if len(text) <= 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def similarity(left: str, right: str) -> float:
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_QUOTE_NOISE = re.compile(r'[\s，,。.、；;：:！!？?（）()【】\[\]"“”\'‘’·|｜]+')


def compact_text(text: str) -> str:
    """去掉空白与标点，只留下可比对的字面内容。"""
    return _QUOTE_NOISE.sub('', _normalize(text).lower())


def quote_matches(quote: str, source: str, *, min_chars: int = 4) -> bool:
    """判断一段「原话证据」是否来自给定的原文。

    先做去标点后的互相包含，再用二元组相似度兜住模型补了「记住，」这类前后缀。
    插件拿不到宿主聊天记录，这个检查只能保证插件内部自洽：
    调用方整体伪造原话仍然挡不住，但拼接外部文本、片段张冠李戴会被拦下来。
    """
    left, right = compact_text(quote), compact_text(source)
    if not left or not right:
        return False
    if left in right:
        return True
    if len(right) >= min_chars and right in left:
        return True
    return similarity(quote, source) >= 0.6


_PREF_PREDICATE = re.compile(
    r'(?:我|本人)?(?P<mod>更|比较|最|特别|有点|不太|不)?'
    r'(?P<verb>喜欢|偏爱|讨厌|不喜欢|习惯|常用|爱吃|过敏|忌口|偏好|受不了|最烦|想要|希望)'
    r'(.{1,80})'
)
_NEGATIVE_PREDICATES = frozenset({'讨厌', '不喜欢', '忌口', '受不了', '最烦', '反感', '恨'})
_POSITIVE_PREDICATES = frozenset({
    '喜欢', '偏爱', '爱吃', '偏好', '习惯', '常用', '想要', '希望', '更', '比较', '最', '特别',
})


def preference_parts(text: str) -> tuple[str, str]:
    """返回（谓词, 对象）；识别不了就返回空串。"""
    match = _PREF_PREDICATE.search(_normalize(text))
    if not match:
        return '', ''
    predicate = (match.group('mod') or '') + match.group('verb')
    obj = match.group(3).strip(' 的了。，！？')
    return predicate, obj


def preference_negated(text: str) -> bool:
    return preference_attitude(text) < 0


def preference_attitude(text: str) -> int:
    """-1 反感、1 偏好、0 识别不出态度。"""
    predicate, _ = preference_parts(text)
    if not predicate:
        return 0
    if '不' in predicate or any(word in predicate for word in _NEGATIVE_PREDICATES):
        return -1
    if any(word in predicate for word in _POSITIVE_PREDICATES):
        return 1
    return 0


_SUBJECT_PATTERN = re.compile(
    r'(?:我|本人|我的)(?:住在|工作在|在|叫|是|用|有|养了|用的是|版本是|地址是|端口是|配置是)(.{1,60})'
)

_DECISION_PATTERN = re.compile(r'(?:我)?(?:决定|计划|打算|准备|约好)(.{1,60})')


_STRONG_NEGATION = re.compile(r'不|没|别|无|未|讨厌|拒绝|忌口|受不了')


def looks_conflicting(old: dict, new_text: str, new_kind: str) -> bool:
    """保守判冲突：偏好同对象异极性，事实同主语异内容，或高相似且否定相反。"""
    old_text = str(old.get('content') or old.get('summary') or '')
    old_kind = str(old.get('kind') or 'event')
    if old_kind == 'event' or new_kind == 'event':
        return False
    if old_kind == 'preference' and new_kind == 'preference':
        old_obj = preference_parts(old_text)[1]
        new_obj = preference_parts(new_text)[1]
        if old_obj and new_obj and similarity(old_obj, new_obj) >= 0.5:
            old_att = preference_attitude(old_text)
            new_att = preference_attitude(new_text)
            if old_att and new_att and old_att != new_att:
                return True
    if similarity(old_text, new_text) < 0.35:
        return False
    old_subject = _SUBJECT_PATTERN.search(old_text)
    new_subject = _SUBJECT_PATTERN.search(new_text)
    if old_subject and new_subject:
        return _normalize(old_text) != _normalize(new_text)
    old_decision = _DECISION_PATTERN.search(old_text)
    new_decision = _DECISION_PATTERN.search(new_text)
    if old_kind == 'decision' and new_kind == 'decision' and old_decision and new_decision:
        return _normalize(old_text) != _normalize(new_text) and similarity(old_text, new_text) >= 0.35
    old_neg = bool(_STRONG_NEGATION.search(old_text))
    new_neg = bool(_STRONG_NEGATION.search(new_text))
    return old_neg != new_neg and similarity(old_text, new_text) >= 0.65


def extract_memory_fragments(text: str, limit: int = 4, min_chars: int = 4,
                             max_chars: int = 160) -> list[dict[str, str]]:
    """从原话里切出可独立保存的句子，并保留逐字证据。"""
    fragments = re.split(r'[。！？；\n]', _normalize(text))
    out: list[dict[str, str]] = []
    for fragment in fragments:
        fragment = fragment.strip()
        if not (min_chars <= len(fragment) <= max_chars):
            continue
        kind = classify_memory(fragment)
        if kind == 'event':
            continue
        out.append({'content': fragment, 'quote': fragment, 'kind': kind})
        if len(out) >= limit:
            break
    return out


def extract_semantics(text: str, limit: int = 3, min_chars: int = 8,
                      max_chars: int = 80) -> list[str]:
    """从锚点里抽可复用的知识点，规则只认「陈述性」句式，宁缺毋滥。"""
    fragments = re.split(r'[。！？；\n]', _normalize(text))
    out: list[str] = []
    for fragment in fragments:
        fragment = fragment.strip()
        if not (min_chars <= len(fragment) <= max_chars):
            continue
        if any(cue in fragment for cue in SEMANTIC_CUES):
            out.append(fragment)
        if len(out) >= limit:
            break
    return out
