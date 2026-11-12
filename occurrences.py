"""把 iCalendar 活动组件展开为指定窗口内的具体场次。

本模块只消费 ``calendar_source.components`` 读到的属性，不猜测未知语法。支持
DAILY/WEEKLY/MONTHLY、INTERVAL、COUNT、UNTIL、BYDAY、BYMONTHDAY、RDATE、
EXDATE 和带 RECURRENCE-ID 的单场覆盖；其余表达会隔离整个系列并说明原因。

场次身份 ``occurrence_id`` 由 UID 和原始 recurrence 起点组成，改期与查询窗口都
不会改变它。实际举行的当地时间放在 ``start``/``end``，改期后只保留新时间。

``collect_occurrences`` 按 UID 分组处理版本：主组件先比 SEQUENCE 再比 DTSTAMP，
单场覆盖按各自 RECURRENCE-ID 分组后同样比较。最高版本并列而内容不同时不按到达
顺序取最后，而是把整个 UID 隔离为版本冲突。
"""

from __future__ import annotations

import calendar as _calendar
import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_GENERATED_PERIODS = 200_000
MAX_WINDOW_DAYS = 3_660

WEEKDAYS = {'MO': 0, 'TU': 1, 'WE': 2, 'TH': 3, 'FR': 4, 'SA': 5, 'SU': 6}
SUPPORTED_FREQUENCIES = {'DAILY', 'WEEKLY', 'MONTHLY'}
UTC = timezone.utc

_DATE_RE = re.compile(r'^(\d{4})(\d{2})(\d{2})$')
_DATETIME_RE = re.compile(r'^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$')
_DURATION_RE = re.compile(r'^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$')
_INT_RE = re.compile(r'^-?\d+$')
_BYDAY_RE = re.compile(r'^([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)$')


# ---------------------------------------------------------------------------
# 隔离原因

class CalendarExpansionError(Exception):
    """单个系列无法安全展开；调用方应隔离该 UID 而不是猜一个结果。"""

    code = 'expansion_error'

    def __init__(self, message, detail=None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_issue(self):
        issue = {'code': self.code, 'message': self.message}
        if self.detail:
            issue['detail'] = str(self.detail)
        return issue


class UnsupportedExpression(CalendarExpansionError):
    code = 'unsupported_expression'


class MissingFloatingTimezone(CalendarExpansionError):
    code = 'missing_floating_timezone'


class UnknownTimezone(CalendarExpansionError):
    code = 'unknown_timezone'


class VersionConflict(CalendarExpansionError):
    code = 'version_conflict'


class InvalidComponent(CalendarExpansionError):
    code = 'invalid_component'


class ExpansionLimit(CalendarExpansionError):
    code = 'expansion_limit'


# ---------------------------------------------------------------------------
# 属性与时间值

def _split_property(property_text):
    """拆出属性名和参数；参数值去掉引号。"""
    pieces = property_text.split(';')
    name = pieces[0].strip().upper()
    if not name:
        raise ValueError('属性名为空')
    params = {}
    for piece in pieces[1:]:
        if '=' not in piece:
            raise ValueError(f'参数缺少等号: {piece}')
        key, value = piece.split('=', 1)
        key = key.strip().upper()
        if key in params:
            raise ValueError(f'参数重复: {key}')
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1].replace('\\"', '"')
        params[key] = value
    return name, params


def _content_fingerprint(rows):
    """忽略版本字段后的内容指纹，用来识别真正的重复而不是并列冲突。"""
    items = []
    for row in rows:
        name, params = _split_property(row['property'])
        if name in {'DTSTAMP', 'SEQUENCE'}:
            continue
        items.append((name, tuple(sorted(params.items())), row['value']))
    items.sort(key=repr)
    return hashlib.sha256(repr(items).encode('utf-8')).hexdigest()


def _lookup_zone(zone_name):
    if not zone_name:
        raise UnknownTimezone('时区名为空')
    if str(zone_name).upper() == 'UTC':
        return UTC
    try:
        return ZoneInfo(str(zone_name))
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise UnknownTimezone(f'未知 IANA 时区: {zone_name}') from error


def _zone_name(tzinfo):
    if tzinfo is UTC:
        return 'UTC'
    return getattr(tzinfo, 'key', None) or str(tzinfo)


@dataclass(frozen=True)
class TimeValue:
    """一个已解析的日期或带时区时刻。

    浮动时间在 :func:`parse_time` 里由来源配置的时区补足，因此到这里总是带时区
    的。``kind == 'date'`` 表示全天活动，值就是本地日期，不能按 UTC 午夜换算。
    """

    kind: str                 # date | datetime
    value: object             # date 或 aware datetime
    zone_name: str | None = None
    floating: bool = False     # 属性本身没有给出 TZID 或 Z，时区来自继承或来源配置


def parse_time(property_name, params, value, floating_timezone, *, inherit_zone=None):
    """解析 DTSTART/DTEND/RDATE/EXDATE/RECURRENCE-ID/UNTIL 的值。

    取值优先级：字面量 ``Z``、属性 ``TZID``、系列内继承的 ``inherit_zone``、
    来源配置的 ``floating_timezone``。都没有就隔离为浮动时间缺时区，不会悄悄按
    UTC 处理。``inherit_zone`` 只用于已经确定时区的系列内部值（RDATE/EXDATE、
    RECURRENCE-ID、DTEND、UNTIL），不会用于主组件的 DTSTART。
    """
    text = str(value).strip()
    value_type = params.get('VALUE', '').upper()
    if value_type == 'PERIOD':
        raise UnsupportedExpression(f'{property_name} 的 VALUE=PERIOD 不支持')
    if value_type not in {'', 'DATE', 'DATE-TIME'}:
        raise UnsupportedExpression(f'{property_name} 的 VALUE={value_type} 不支持')

    date_match = _DATE_RE.match(text)
    if date_match:
        if value_type == 'DATE-TIME':
            raise InvalidComponent(f'{property_name} 声明 DATE-TIME 但值是日期')
        year, month, day = (int(part) for part in date_match.groups())
        try:
            return TimeValue('date', date(year, month, day))
        except ValueError as error:
            raise InvalidComponent(f'{property_name} 日期无效: {text}') from error

    datetime_match = _DATETIME_RE.match(text)
    if not datetime_match:
        raise InvalidComponent(f'{property_name} 时间格式不支持: {text}')
    if value_type == 'DATE':
        raise InvalidComponent(f'{property_name} 声明 DATE 但值是时间')

    parts = datetime_match.groups()
    year, month, day, hour, minute, second = (int(part) for part in parts[:6])
    utc_marker = parts[6] == 'Z'
    tzid = params.get('TZID')
    if utc_marker and tzid:
        raise InvalidComponent(f'{property_name} 同时给出 Z 和 TZID')

    if utc_marker:
        zone, zone_name, floating = UTC, 'UTC', False
    elif tzid:
        zone, zone_name, floating = _lookup_zone(tzid), tzid, False
    elif inherit_zone is not None:
        zone, zone_name, floating = inherit_zone, _zone_name(inherit_zone), True
    elif floating_timezone:
        zone, zone_name, floating = _lookup_zone(floating_timezone), str(floating_timezone), True
    else:
        raise MissingFloatingTimezone(
            f'{property_name}={text} 是浮动时间，来源未配置时区')

    try:
        aware = datetime(year, month, day, hour, minute, second, tzinfo=zone)
    except ValueError as error:
        raise InvalidComponent(f'{property_name} 时间无效: {text}') from error
    return TimeValue('datetime', aware, zone_name, floating)


def _local_key(time_value, series_zone):
    """recurrence 身份键：系列时区的当地墙钟值，和查询窗口无关。"""
    if time_value.kind == 'date':
        return time_value.value.strftime('%Y%m%d')
    return time_value.value.astimezone(series_zone).strftime('%Y%m%dT%H%M%S')


def _display_recurrence_key(key):
    if 'T' in key:
        return (f'{key[:4]}-{key[4:6]}-{key[6:8]}T'
                f'{key[9:11]}:{key[11:13]}:{key[13:15]}')
    return f'{key[:4]}-{key[4:6]}-{key[6:8]}'


def _parse_sequence(value):
    text = str(value).strip()
    if not _INT_RE.match(text) or int(text) < 0:
        raise InvalidComponent(f'SEQUENCE 无效: {value}')
    return int(text)


def _parse_stamp_key(value):
    match = _DATETIME_RE.match(str(value).strip())
    if not match:
        raise InvalidComponent(f'DTSTAMP 格式不支持: {value}')
    return ''.join(match.groups()[:6]) + 'Z'


def _parse_duration(value):
    text = str(value).strip().upper()
    if text.startswith('-'):
        raise InvalidComponent(f'DURATION 不能为负: {value}')
    match = _DURATION_RE.match(text)
    if not match:
        raise UnsupportedExpression('DURATION 表达不支持', detail=value)
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    if not (days or hours or minutes or seconds):
        raise InvalidComponent(f'DURATION 不能为 0: {value}')
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


# ---------------------------------------------------------------------------
# 重复规则

@dataclass(frozen=True)
class RRule:
    freq: str
    interval: int = 1
    count: int | None = None
    until: TimeValue | None = None
    byday: tuple = ()
    bymonthday: tuple = ()


def _precheck_rrule(text):
    """先做字段级检查，让未支持表达优先于浮动时间等后续问题被报告。"""
    parts = {}
    for piece in str(text).split(';'):
        if not piece:
            continue
        if '=' not in piece:
            raise InvalidComponent(f'RRULE 片段缺少等号: {piece}')
        key, value = piece.split('=', 1)
        key, value = key.strip().upper(), value.strip().upper()
        if key in parts:
            raise InvalidComponent(f'RRULE 重复字段: {key}')
        parts[key] = value
    supported = {'FREQ', 'INTERVAL', 'COUNT', 'UNTIL', 'BYDAY', 'BYMONTHDAY'}
    extra = set(parts) - supported
    if extra:
        raise UnsupportedExpression(
            f'RRULE 含未支持字段: {",".join(sorted(extra))}', detail=text)
    freq = parts.get('FREQ')
    if not freq:
        raise InvalidComponent('RRULE 缺少 FREQ')
    if freq not in SUPPORTED_FREQUENCIES:
        raise UnsupportedExpression(f'RRULE 频率 {freq} 未支持', detail=text)
    return parts


def _parse_rrule(text, start_value, floating_timezone):
    """只接受任务列出的规则；出现任何额外表达都隔离整个系列。"""
    parts = _precheck_rrule(text)
    freq = parts['FREQ']

    interval = 1
    if 'INTERVAL' in parts:
        if not _INT_RE.match(parts['INTERVAL']) or int(parts['INTERVAL']) < 1:
            raise InvalidComponent(f'RRULE INTERVAL 无效: {parts["INTERVAL"]}')
        interval = int(parts['INTERVAL'])

    count = None
    if 'COUNT' in parts:
        if not _INT_RE.match(parts['COUNT']) or int(parts['COUNT']) < 1:
            raise InvalidComponent(f'RRULE COUNT 无效: {parts["COUNT"]}')
        count = int(parts['COUNT'])
    if count is not None and 'UNTIL' in parts:
        raise InvalidComponent('RRULE 同时给出 COUNT 和 UNTIL，结束边界有歧义')

    until = None
    if 'UNTIL' in parts:
        until = parse_time('RRULE UNTIL', {}, parts['UNTIL'], floating_timezone,
                           inherit_zone=start_value.value.tzinfo
                           if start_value.kind == 'datetime' else None)
        if start_value.kind == 'date' and until.kind != 'date':
            raise InvalidComponent('全天系列的 UNTIL 必须是日期')
        if start_value.kind == 'datetime' and until.kind != 'datetime':
            raise InvalidComponent('定时系列的 UNTIL 必须是时间')

    byday = ()
    if 'BYDAY' in parts:
        if freq == 'DAILY':
            raise UnsupportedExpression('DAILY 与 BYDAY 组合未支持', detail=text)
        raw = [item.strip() for item in parts['BYDAY'].split(',') if item.strip()]
        parsed = []
        for item in raw:
            match = _BYDAY_RE.match(item)
            if not match:
                raise InvalidComponent(f'RRULE BYDAY 无效: {item}')
            ordinal_text, weekday = match.groups()
            ordinal = int(ordinal_text) if ordinal_text else None
            if ordinal is not None:
                if freq != 'MONTHLY':
                    raise UnsupportedExpression(
                        '带序号的 BYDAY 仅支持 MONTHLY', detail=text)
                if ordinal == 0 or abs(ordinal) > 5:
                    raise InvalidComponent(f'RRULE BYDAY 序号越界: {item}')
            parsed.append((ordinal, WEEKDAYS[weekday]))
        byday = tuple(parsed)

    bymonthday = ()
    if 'BYMONTHDAY' in parts:
        if freq in {'WEEKLY', 'DAILY'}:
            raise UnsupportedExpression(
                f'{freq} 与 BYMONTHDAY 组合未支持', detail=text)
        raw = [item.strip() for item in parts['BYMONTHDAY'].split(',') if item.strip()]
        values = []
        for item in raw:
            if not _INT_RE.match(item):
                raise InvalidComponent(f'RRULE BYMONTHDAY 无效: {item}')
            number = int(item)
            if number == 0 or abs(number) > 31:
                raise InvalidComponent(f'RRULE BYMONTHDAY 越界: {item}')
            values.append(number)
        bymonthday = tuple(values)

    return RRule(freq=freq, interval=interval, count=count, until=until,
                 byday=byday, bymonthday=bymonthday)


def _month_candidates(anchor_day, year, month, rule):
    """某个月候选日期；没有 BY 字段时保持系列起始日号，缺日就跳过该月。"""
    last_day = _calendar.monthrange(year, month)[1]
    if not rule.byday and not rule.bymonthday:
        return [anchor_day] if anchor_day <= last_day else []

    wanted = set()
    if rule.bymonthday:
        for number in rule.bymonthday:
            day = number if number > 0 else last_day + 1 + number
            if 1 <= day <= last_day:
                wanted.add(day)
    if rule.byday:
        weekday_days = set()
        for ordinal, weekday in rule.byday:
            candidates = [day for day in range(1, last_day + 1)
                          if date(year, month, day).weekday() == weekday]
            if ordinal is None:
                weekday_days.update(candidates)
                continue
            index = ordinal - 1 if ordinal > 0 else ordinal
            if -len(candidates) <= index < len(candidates):
                weekday_days.add(candidates[index])
        wanted = wanted & weekday_days if rule.bymonthday else weekday_days
    return sorted(wanted)


def _generate_rule(start_value, rule, hard_stop):
    """从系列起点生成候选起点；COUNT 永远从起点数，不从查询下界重数。"""
    all_day = start_value.kind == 'date'
    zone = None if all_day else start_value.value.tzinfo
    anchor_date = start_value.value if all_day else start_value.value.date()
    slot_time = None if all_day else start_value.value.timetz().replace(tzinfo=None)
    anchor_day = anchor_date.day
    emitted = 0
    periods = 0
    period_limit = MAX_GENERATED_PERIODS
    while True:
        periods += 1
        if periods > period_limit:
            raise ExpansionLimit('规则推进超出安全上限', detail={'freq': rule.freq})

        if rule.freq == 'DAILY':
            dates = [anchor_date + timedelta(days=(periods - 1) * rule.interval)]
        elif rule.freq == 'WEEKLY':
            week_start = anchor_date - timedelta(days=anchor_date.weekday())
            week_start += timedelta(weeks=(periods - 1) * rule.interval)
            if rule.byday:
                dates = [week_start + timedelta(days=weekday)
                         for _, weekday in sorted(rule.byday, key=lambda item: item[1])]
            else:
                dates = [week_start + timedelta(days=anchor_date.weekday())]
        else:
            month_offset = (periods - 1) * rule.interval
            year, month = anchor_date.year, anchor_date.month + month_offset
            year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
            dates = [date(year, month, day)
                     for day in _month_candidates(anchor_day, year, month, rule)]

        cut_off = False
        for candidate_date in sorted(dates):
            if candidate_date < anchor_date:
                continue
            candidate = (candidate_date if all_day
                         else datetime.combine(candidate_date, slot_time, tzinfo=zone))
            if rule.until is not None and candidate > rule.until.value:
                cut_off = True
                break
            yield candidate
            emitted += 1
            if rule.count is not None and emitted >= rule.count:
                return

        latest = max(dates) if dates else anchor_date
        if cut_off or latest > hard_stop:
            return


# ---------------------------------------------------------------------------
# 查询窗口与场次时间

@dataclass(frozen=True)
class Window:
    """左闭右开的绝对时间窗口，按场次实际开始时刻判断是否落在窗口内。"""

    start: datetime
    end: datetime

    def __post_init__(self):
        for name in ('start', 'end'):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f'窗口 {name} 必须是带时区的时刻')
        if self.start >= self.end:
            raise ValueError('窗口起点必须早于终点')
        if (self.end - self.start).days > MAX_WINDOW_DAYS:
            raise ValueError(f'窗口不能超过 {MAX_WINDOW_DAYS} 天')

    def contains(self, instant):
        return self.start <= instant < self.end

    @classmethod
    def from_dates(cls, start_date, end_date, zone_name='UTC'):
        """日期区间转窗口：起点当天 00:00，包含终点当天。"""
        zone = _lookup_zone(zone_name)
        if end_date < start_date:
            raise ValueError('窗口结束日期早于开始日期')
        return cls(
            datetime.combine(start_date, time(0), tzinfo=zone),
            datetime.combine(end_date + timedelta(days=1), time(0), tzinfo=zone),
        )


def _window_dates(window, zone):
    return (window.start.astimezone(zone).date() - timedelta(days=1),
            window.end.astimezone(zone).date() + timedelta(days=1))


def _start_instant(value, window_zone):
    """场次开始的绝对时刻；全天活动按窗口时区的本地午夜解释，不按 UTC 午夜。"""
    if value.kind == 'date':
        return datetime.combine(value.value, time(0), tzinfo=window_zone)
    return value.value


def _wall_span(start, end):
    """同一引用组件内 DTEND-DTSTART 的当地墙钟时长。"""
    return end.replace(tzinfo=None) - start.replace(tzinfo=None)


def _end_for_occurrence(start_value, reference_start, end_value, duration,
                        all_day, zone):
    """每个场次沿用引用组件的当地墙钟时长，而不是把 DTEND 当成固定绝对时刻。"""
    if all_day:
        if end_value is not None:
            days = (end_value.value - reference_start.value).days
        elif duration is not None and duration.seconds == 0:
            days = duration.days
        else:
            days = 1
        return start_value.value + timedelta(days=max(days, 1))
    if end_value is not None:
        wall = _wall_span(reference_start.value, end_value.value)
        naive = start_value.value.replace(tzinfo=None) + wall
        return naive.replace(tzinfo=zone)
    if duration is not None:
        if duration.seconds == 0 and duration.microseconds == 0:
            naive = start_value.value.replace(tzinfo=None) + duration
            return naive.replace(tzinfo=zone)
        return start_value.value + duration
    return start_value.value


def _duration_of(component):
    row = component.single('DURATION')
    return _parse_duration(row['value']) if row is not None else None


def _master_times(master, floating_timezone, window_zone):
    if master.single('DTEND') is not None and master.single('DURATION') is not None:
        raise InvalidComponent(f'{master.uid} 同时给出 DTEND 和 DURATION')
    start_row = master.single('DTSTART')
    if start_row is None:
        raise InvalidComponent(f'{master.uid} 主组件缺少 DTSTART')
    start_value = parse_time('DTSTART', start_row['params'], start_row['value'],
                             floating_timezone)
    if start_value.kind == 'datetime':
        series_zone = (_lookup_zone(start_value.zone_name)
                       if start_value.zone_name else _lookup_zone(floating_timezone))
    else:
        # 全天活动本身没有时刻，不做 UTC 午夜换算；显示时区按窗口时区解释。
        series_zone = window_zone
    end_row = master.single('DTEND')
    end_value = None
    if end_row is not None:
        end_value = parse_time('DTEND', end_row['params'], end_row['value'],
                               floating_timezone, inherit_zone=series_zone)
        if end_value.kind != start_value.kind:
            raise InvalidComponent(f'{master.uid} 的 DTSTART 与 DTEND 类型不一致')
        if (end_value.kind == 'datetime' and end_value.value < start_value.value):
            raise InvalidComponent(f'{master.uid} 的 DTEND 早于 DTSTART')
        if end_value.kind == 'date' and end_value.value < start_value.value:
            raise InvalidComponent(f'{master.uid} 的 DTEND 早于 DTSTART')
    return start_value, end_value, _duration_of(master), series_zone


# ---------------------------------------------------------------------------
# 组件版本

@dataclass
class Component:
    """一个 VEVENT 的规范化视图；``rows`` 保留原文用于指纹与追溯。"""

    uid: str
    rows: list
    props: dict
    sequence: int
    stamp_key: str
    fingerprint: str
    recurrence_row: dict | None
    recurrence: TimeValue | None = None

    def values(self, name):
        return self.props.get(name, [])

    def single(self, name):
        values = self.values(name)
        if len(values) > 1:
            raise InvalidComponent(f'{self.uid} 出现多个 {name}')
        return values[0] if values else None

    def summary(self):
        value = self.single('SUMMARY')
        return value['value'] if value else None

    def status(self):
        value = self.single('STATUS')
        return value['value'].strip().upper() if value else ''

    def version_key(self):
        return (self.sequence, self.stamp_key)


def _raw_uid(rows):
    """尽力从无法规范化的组件里取出 UID，把隔离记录挂到正确的系列上。"""
    for row in rows:
        try:
            name, _ = _split_property(row['property'])
        except ValueError:
            continue
        if name == 'UID':
            return row['value'].strip() or None
    return None


def _parse_component(rows):
    props = {}
    for row in rows:
        try:
            name, params = _split_property(row['property'])
        except ValueError as error:
            raise InvalidComponent(str(error)) from error
        props.setdefault(name, []).append({'params': params, 'value': row['value']})

    uid_values = props.get('UID', [])
    if len(uid_values) != 1 or not uid_values[0]['value'].strip():
        raise InvalidComponent('活动必须恰有一个非空 UID')
    uid = uid_values[0]['value'].strip()

    sequence = 0
    sequence_rows = props.get('SEQUENCE', [])
    if len(sequence_rows) > 1:
        raise InvalidComponent(f'{uid} 出现多个 SEQUENCE')
    if sequence_rows:
        sequence = _parse_sequence(sequence_rows[0]['value'])

    stamp_rows = props.get('DTSTAMP', [])
    if len(stamp_rows) > 1:
        raise InvalidComponent(f'{uid} 出现多个 DTSTAMP')
    stamp_key = _parse_stamp_key(stamp_rows[0]['value']) if stamp_rows else ''

    recurrence_rows = props.get('RECURRENCE-ID', [])
    if len(recurrence_rows) > 1:
        raise InvalidComponent(f'{uid} 出现多个 RECURRENCE-ID')

    return Component(
        uid=uid,
        rows=rows,
        props=props,
        sequence=sequence,
        stamp_key=stamp_key,
        fingerprint=_content_fingerprint(rows),
        recurrence_row=recurrence_rows[0] if recurrence_rows else None,
    )


def _select_version(components, label):
    """先 SEQUENCE，再 DTSTAMP；并列且内容不同即冲突，不按到达顺序取最后。"""
    if not components:
        return None
    best = max(component.version_key() for component in components)
    tied = [component for component in components if component.version_key() == best]
    if len({component.fingerprint for component in tied}) > 1:
        raise VersionConflict(
            f'{label} 在相同 SEQUENCE 与 DTSTAMP 下出现不同内容',
            detail={
                'versions': [
                    {'sequence': item.sequence, 'dtstamp': item.stamp_key,
                     'fingerprint': item.fingerprint[:16]}
                    for item in tied
                ]
            },
        )
    return tied[0]


# ---------------------------------------------------------------------------
# 系列展开

def _parse_time_list(name, row, floating_timezone, inherit_zone=None):
    """RDATE/EXDATE 允许同一行逗号分隔多条。"""
    values = []
    for piece in row['value'].split(','):
        piece = piece.strip()
        if not piece:
            continue
        values.append(parse_time(name, row['params'], piece, floating_timezone,
                                 inherit_zone=inherit_zone))
    return values


def _resolve_overrides(uid, components, series_zone, floating_timezone):
    """每条 RECURRENCE-ID 是独立身份，各自比 SEQUENCE 再比 DTSTAMP。"""
    grouped = {}
    for component in components:
        row = component.recurrence_row
        if row['params'].get('RANGE'):
            # RANGE=THISANDFUTURE 会改写后续所有场次，属于本轮未支持的扩展语义。
            raise UnsupportedExpression(
                f'{uid} 的 RECURRENCE-ID 含 RANGE={row["params"]["RANGE"]}')
        value = parse_time('RECURRENCE-ID', row['params'], row['value'],
                           floating_timezone, inherit_zone=series_zone)
        component.recurrence = value
        grouped.setdefault(_local_key(value, series_zone), []).append(component)
    resolved = {}
    for key in sorted(grouped):
        resolved[key] = _select_version(grouped[key], f'{uid} 单场覆盖 {key}')
    return resolved


def _occurrence_dict(uid, key, start, end, series_zone, all_day, *,
                     moved=False, summary=None, floating=False):
    return {
        'occurrence_id': f'{uid}@{key}',
        'uid': uid,
        'recurrence_key': _display_recurrence_key(key),
        'start': start.isoformat() if all_day else start.astimezone(series_zone).isoformat(),
        'end': end.isoformat() if all_day else end.astimezone(series_zone).isoformat(),
        'timezone': None if all_day else _zone_name(series_zone),
        'all_day': all_day,
        'moved': moved,
        'summary': summary,
        'floating_assumed': floating,
        '_sort_at': _start_instant(
            TimeValue('date', start) if all_day else TimeValue('datetime', start),
            UTC if all_day else series_zone),
    }


def _issue(uid, error):
    issue = {'uid': uid}
    issue.update(error.as_issue())
    return issue


def _expand_series(uid, masters, override_components, floating_timezone,
                   window, window_zone):
    master = _select_version(masters, f'{uid} 主组件')
    if master is None:
        raise InvalidComponent(f'{uid} 只有单场覆盖没有主组件')
    if master.status() == 'CANCELLED':
        # 整系列取消：主组件 STATUS:CANCELLED，不再输出任何场次。
        return []

    rrules = master.values('RRULE')
    if len(rrules) > 1:
        raise UnsupportedExpression(f'{uid} 给出多条 RRULE')
    if rrules:
        # 规则表达检查放在主组件时间解析之前，未支持规则应优先作为隔离原因。
        _precheck_rrule(rrules[0]['value'])

    start_value, end_value, duration, series_zone = _master_times(
        master, floating_timezone, window_zone)
    all_day = start_value.kind == 'date'

    rdate_rows = master.values('RDATE')

    hard_stop = _window_dates(window, series_zone)[1]
    slots = {}
    if not rrules and not rdate_rows:
        # 单场活动不是错误：它只有 DTSTART 这一个场次。
        slots[_local_key(start_value, series_zone)] = start_value
    if rrules:
        rule = _parse_rrule(rrules[0]['value'], start_value, floating_timezone)
        for candidate in _generate_rule(start_value, rule, hard_stop):
            # 由规则扩展出来的场次直接继承了系列时区，不算浮动时间补足。
            time_value = (TimeValue('date', candidate) if all_day
                          else TimeValue('datetime', candidate,
                                         start_value.zone_name))
            slots.setdefault(_local_key(time_value, series_zone), time_value)

    for row in rdate_rows:
        for value in _parse_time_list('RDATE', row, floating_timezone,
                                      inherit_zone=series_zone):
            if value.kind != start_value.kind:
                raise InvalidComponent(f'{uid} 的 RDATE 与 DTSTART 类型不一致')
            slots.setdefault(_local_key(value, series_zone), value)

    # EXDATE 只移除单场，不影响系列其余场次。
    for row in master.values('EXDATE'):
        for value in _parse_time_list('EXDATE', row, floating_timezone,
                                      inherit_zone=series_zone):
            if value.kind != start_value.kind:
                raise InvalidComponent(f'{uid} 的 EXDATE 与 DTSTART 类型不一致')
            slots.pop(_local_key(value, series_zone), None)

    overrides = _resolve_overrides(uid, override_components, series_zone,
                                   floating_timezone)
    # 被覆盖的 recurrence 身份由覆盖接管，旧时刻在输出阶段不会再出现，也不会
    # 与恰好同一时刻的另一个 UID 场次合并。
    for key in overrides:
        slots.pop(key, None)

    result = []
    for key in sorted(slots):
        start = slots[key]
        if not window.contains(_start_instant(start, window_zone)):
            continue
        end = _end_for_occurrence(start, start_value, end_value, duration,
                                  all_day, series_zone)
        result.append((_start_instant(start, window_zone), _occurrence_dict(
            uid, key, start.value, end, series_zone, all_day,
            summary=master.summary(),
            floating=start_value.floating)))

    for key in sorted(overrides):
        override = overrides[key]
        if override.status() == 'CANCELLED':
            # 单场取消只影响这一个 recurrence 身份。
            continue
        if override.single('DTEND') is not None and override.single('DURATION') is not None:
            raise InvalidComponent(f'{uid} 的覆盖 {key} 同时给出 DTEND 和 DURATION')
        start_row = override.single('DTSTART')
        if start_row is None:
            # 覆盖没给 DTSTART 时沿用原 recurrence 的墙钟时刻，而不是旧日期。
            recurrence_value = override.recurrence
            if recurrence_value.kind != start_value.kind:
                raise InvalidComponent(f'{uid} 的单场覆盖时间类型不一致')
            start = TimeValue(recurrence_value.kind, recurrence_value.value,
                              recurrence_value.zone_name, start_value.floating)
        else:
            start = parse_time('DTSTART', start_row['params'], start_row['value'],
                               floating_timezone, inherit_zone=series_zone)
            if start.kind != start_value.kind:
                raise InvalidComponent(f'{uid} 的单场覆盖改变了时间类型')

        override_end_value = end_value
        override_duration = duration
        end_row = override.single('DTEND')
        if end_row is not None:
            override_end_value = parse_time('DTEND', end_row['params'],
                                            end_row['value'], floating_timezone,
                                            inherit_zone=series_zone)
            if override_end_value.kind != start.kind:
                raise InvalidComponent(f'{uid} 的单场覆盖 DTEND 类型不一致')
            override_duration = None
        elif override.single('DURATION') is not None:
            override_duration = _parse_duration(override.single('DURATION')['value'])
            override_end_value = None

        if not window.contains(_start_instant(start, window_zone)):
            # 改期移入窗口的场次会在这里被纳入；移出窗口的旧时刻不会留下。
            continue
        # 覆盖自己给出 DTEND 时以它的 DTSTART 为参照；沿用主组件时长时以主组件
        # 的 DTSTART 为参照，保证墙钟时长一致。
        reference_start = start if end_row is not None else start_value
        end = _end_for_occurrence(start, reference_start, override_end_value,
                                  override_duration, all_day, series_zone)
        result.append((_start_instant(start, window_zone), _occurrence_dict(
            uid, key, start.value, end, series_zone, all_day, moved=True,
            summary=override.summary() or master.summary(),
            floating=start_value.floating)))

    result.sort(key=lambda pair: (pair[0], pair[1]['occurrence_id']))
    return [item for _, item in result]


def collect_occurrences(rows, window, *, floating_timezone=None, window_zone=None):
    """把 ``calendar_source.components`` 的属性展开为窗口内的具体场次。

    返回 ``{"occurrences": [...], "quarantined": [...]}``。隔离按 UID 整组处理；
    缺少某个覆盖组件不会生成取消，只有显式 STATUS:CANCELLED 或 EXDATE 才会移除
    场次。同一订阅重跑两次得到完全相同的 occurrence_id 集合。
    """
    if not isinstance(window, Window):
        raise TypeError('window 必须是 Window')
    if window_zone is None:
        window_zone = window.start.tzinfo or UTC

    groups = {}
    for component_rows in rows:
        try:
            component = _parse_component(component_rows)
        except CalendarExpansionError as error:
            uid = _raw_uid(component_rows) or '(无 UID)'
            group = groups.setdefault(uid, {'masters': [], 'overrides': [], 'error': None})
            group['error'] = group['error'] or error
            continue
        group = groups.setdefault(component.uid,
                                  {'masters': [], 'overrides': [], 'error': None})
        if component.recurrence_row is None:
            group['masters'].append(component)
        else:
            group['overrides'].append(component)

    occurrences = []
    quarantined = []
    for uid in sorted(groups):
        group = groups[uid]
        if group['error'] is not None:
            quarantined.append(_issue(uid, group['error']))
            continue
        try:
            expanded = _expand_series(uid, group['masters'], group['overrides'],
                                      floating_timezone, window, window_zone)
        except CalendarExpansionError as error:
            quarantined.append(_issue(uid, error))
            continue
        occurrences.extend(expanded)

    occurrences.sort(key=lambda item: (item['_sort_at'], item['occurrence_id']))
    for item in occurrences:
        item.pop('_sort_at', None)
    return {'occurrences': occurrences, 'quarantined': quarantined}
