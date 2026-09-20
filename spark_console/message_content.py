"""Bounded, task-local content options. Never use account data in API requests."""
import json
import random
import time
from datetime import timezone
from zoneinfo import ZoneInfo

import requests

PREFIX = '@spark-content:'
CATEGORIES = set('abcdefghijkl')
FALLBACK = '今日火花，祝你今天开心！'


def parse_content(value):
    if not value.startswith(PREFIX):
        return {'mode': 'fixed', 'text': value}
    try:
        spec = json.loads(value[len(PREFIX):])
        if not isinstance(spec, dict) or spec.get('mode') not in {'hitokoto', 'random', 'daily'}:
            raise ValueError()
        text = spec.get('text', '')
        fallback = spec.get('fallback', FALLBACK)
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 400:
            raise ValueError()
        if not isinstance(fallback, str) or not 1 <= len(fallback.strip()) <= 200:
            raise ValueError()
        if spec['mode'] == 'hitokoto':
            types = spec.get('types', ['i'])
            if not isinstance(types, list) or not 1 <= len(types) <= 12 or any(not isinstance(t, str) or t not in CATEGORIES for t in types):
                raise ValueError()
            if text.count('{一言}') != 1:
                raise ValueError()
        elif not 2 <= len([line for line in text.splitlines() if line.strip()]) <= 20:
            raise ValueError()
        return dict(spec, text=text.strip(), fallback=fallback.strip())
    except (ValueError, TypeError, KeyError):
        raise ValueError('多样化文案无效：一言模板须含一个 {一言}；文案库须有 2–20 行；配置总长不超过 500 字') from None


def describe_content(value):
    try:
        spec = parse_content(value)
        labels = {'fixed':'', 'hitokoto':'[随机一言] ', 'random':'[随机文案库] ', 'daily':'[按天轮换] '}
        return labels[spec['mode']] + spec['text']
    except ValueError:
        return '文案配置无效，请编辑任务'


def fetch_quote(types):
    # Fixed destination, no redirects, no credentials, bounded time/body/length.
    deadline = time.monotonic() + 4
    with requests.get('https://v1.hitokoto.cn/', params=[('c', t) for t in types],
                      timeout=(2, 3), allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            raise ValueError('quote unavailable')
        body = b''
        for block in response.iter_content(1):
            body += block
            if len(body) > 8192 or time.monotonic() > deadline:
                raise ValueError('quote too large')
        data = json.loads(body)
        value = data.get('hitokoto')
        if not isinstance(value, str):
            raise ValueError('invalid quote')
        value = ' '.join(value.split())
        if not 1 <= len(value) <= 200 or any(ord(c) < 32 for c in value):
            raise ValueError('invalid quote')
        def metadata(key, unknown):
            part = data.get(key)
            if not isinstance(part, str) or not part.strip():
                return unknown
            part = ' '.join(part.split())
            if len(part) > 80 or any(ord(c) < 32 for c in part):
                return unknown
            return part
        source = metadata('from', '未知来源')
        author = metadata('from_who', '未知作者')
        return f'{value}\n—— {source}（{author}）'


def render_content(template, scheduled_for, *, quote=fetch_quote):
    spec = parse_content(template)
    mode, text = spec['mode'], spec['text']
    if mode == 'fixed':
        return text, '固定文案'
    if mode in {'random', 'daily'}:
        choices = [line.strip() for line in text.splitlines() if line.strip()]
        when = scheduled_for if scheduled_for.tzinfo else scheduled_for.replace(tzinfo=timezone.utc)
        index = when.astimezone(ZoneInfo('Asia/Shanghai')).date().toordinal() % len(choices)
        return (random.SystemRandom().choice(choices) if mode == 'random' else choices[index]), ('随机文案库' if mode == 'random' else '按天轮换文案库')
    try:
        rendered = text.replace('{一言}', quote(spec.get('types', ['i'])))
        if not 1 <= len(rendered) <= 500:
            raise ValueError('message too long')
        return rendered, '一言（第三方内容）'
    except Exception:
        return spec['fallback'], '一言不可用，已使用备用文案'
