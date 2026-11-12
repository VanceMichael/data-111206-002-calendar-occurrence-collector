import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from calendar_source import components, read
from occurrences import Window, collect_occurrences


def make(*events):
    body = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Local Trade//Tests//ZH']
    for event in events:
        body += ['BEGIN:VEVENT'] + event + ['END:VEVENT']
    body.append('END:VCALENDAR')
    return components(('\r\n'.join(body) + '\r\n').encode())


def window(start='2026-10-01', end='2026-12-31', zone='America/New_York'):
    return Window.from_dates(date.fromisoformat(start), date.fromisoformat(end), zone)


def ids(result):
    return [item['occurrence_id'] for item in result['occurrences']]


def by_id(result, occurrence_id):
    return next(item for item in result['occurrences']
                if item['occurrence_id'] == occurrence_id)


def episodes(result, uid):
    return [item for item in result['occurrences'] if item['uid'] == uid]


class FixtureTests(unittest.TestCase):
    def test_calendar_fixture_expands_to_local_times(self):
        _, rows = read('fixtures/calendar.ics')
        result = collect_occurrences(rows, window())
        self.assertEqual(result['quarantined'], [])
        self.assertEqual(ids(result), [
            'roadshow-alpha@20261025T090000',
            'roadshow-alpha@20261101T090000',
            'roadshow-alpha@20261115T090000',
        ])
        self.assertEqual(by_id(result, 'roadshow-alpha@20261025T090000')['start'],
                         '2026-10-25T09:00:00-04:00')
        # 11 月 1 日是夏令时结束日，09:00 应为 -05:00。
        moved = by_id(result, 'roadshow-alpha@20261101T090000')
        self.assertEqual(moved['start'], '2026-11-02T14:00:00-05:00')
        self.assertEqual(moved['end'], '2026-11-02T15:00:00-05:00')
        self.assertTrue(moved['moved'])
        # 11 月 8 日被 EXDATE 移除，只剩 COUNT 数到的最后一场。
        self.assertNotIn('roadshow-alpha@20261108T090000', ids(result))

    def test_all_day_monthly_skips_missing_days(self):
        _, rows = read('fixtures/calendar.ics')
        result = collect_occurrences(rows, window('2026-01-01', '2026-12-31'))
        monthly = episodes(result, 'monthly-beta')
        self.assertEqual([item['start'] for item in monthly],
                         ['2026-01-31', '2026-03-31', '2026-05-31', '2026-07-31'])
        self.assertTrue(all(item['all_day'] for item in monthly))
        self.assertEqual(monthly[0]['end'], '2026-02-01')
        self.assertIsNone(monthly[0]['timezone'])

    def test_revisions_quarantine_unsupported_series(self):
        _, rows = read('fixtures/revisions.ics')
        result = collect_occurrences(rows, window(), floating_timezone='America/New_York')
        quarantined = {item['uid']: item for item in result['quarantined']}
        self.assertEqual(quarantined['unsupported-gamma']['code'], 'unsupported_expression')
        self.assertIn('BYWEEKNO', quarantined['unsupported-gamma']['message'])
        self.assertEqual(quarantined['conflict-epsilon']['code'], 'version_conflict')
        self.assertEqual(episodes(result, 'unsupported-gamma'), [])
        self.assertEqual(episodes(result, 'conflict-epsilon'), [])

    def test_unsupported_series_is_reported_even_without_floating_zone(self):
        _, rows = read('fixtures/revisions.ics')
        result = collect_occurrences(rows, window())
        quarantined = {item['uid']: item for item in result['quarantined']}
        self.assertEqual(quarantined['unsupported-gamma']['code'], 'unsupported_expression')

    def test_floating_times_need_configured_zone(self):
        _, rows = read('fixtures/revisions.ics')
        result = collect_occurrences(rows, window())
        quarantined = {item['uid']: item for item in result['quarantined']}
        self.assertEqual(quarantined['daily-delta']['code'], 'missing_floating_timezone')

        result = collect_occurrences(rows, window(), floating_timezone='America/New_York')
        delta = episodes(result, 'daily-delta')
        self.assertEqual([item['start'] for item in delta],
                         ['2026-11-01T09:00:00-05:00', '2026-11-03T09:00:00-05:00',
                          '2026-11-07T09:00:00-05:00', '2026-11-09T09:00:00-05:00',
                          '2026-11-11T09:00:00-05:00', '2026-11-12T09:00:00-05:00'])
        self.assertTrue(all(item['floating_assumed'] for item in delta))

    def test_old_version_before_and_after_new_version(self):
        _, calendar_rows = read('fixtures/calendar.ics')
        _, revision_rows = read('fixtures/revisions.ics')
        for rows in (revision_rows + calendar_rows, calendar_rows + revision_rows):
            result = collect_occurrences(rows, window('2026-09-01', '2027-03-01'))
            alpha = episodes(result, 'roadshow-alpha')
            # 新主组件 COUNT=4 只到 11 月 15 日，11 月 8 日被 EXDATE 移除，
            # 11 月 1 日改期、11 月 15 日被 SEQUENCE 3 的单场取消移除。
            self.assertEqual([item['occurrence_id'] for item in alpha], [
                'roadshow-alpha@20261025T090000',
                'roadshow-alpha@20261101T090000',
            ])
            self.assertEqual(alpha[0]['start'], '2026-10-25T09:00:00-04:00')
            self.assertEqual(alpha[1]['start'], '2026-11-02T14:00:00-05:00')

    def test_single_cancel_from_revisions_is_applied(self):
        _, calendar_rows = read('fixtures/calendar.ics')
        _, revision_rows = read('fixtures/revisions.ics')
        result = collect_occurrences(revision_rows + calendar_rows,
                                     window('2026-09-01', '2027-03-01'))
        alpha = episodes(result, 'roadshow-alpha')
        # SEQUENCE 3 取消 11 月 15 日那场；旧版的 08:00 场次不应复活。
        self.assertNotIn('roadshow-alpha@20261115T090000',
                         [item['occurrence_id'] for item in alpha])
        self.assertEqual([item['start'] for item in alpha],
                         ['2026-10-25T09:00:00-04:00', '2026-11-02T14:00:00-05:00'])
        self.assertEqual(episodes(result, 'unsupported-gamma'), [])

    def test_rerun_is_stable(self):
        _, rows = read('fixtures/calendar.ics')
        first = collect_occurrences(rows, window())
        second = collect_occurrences(rows, window())
        self.assertEqual(ids(first), ids(second))
        self.assertEqual(len(ids(first)), len(set(ids(first))))


class VersionTests(unittest.TestCase):
    def test_sequence_beats_dtstamp(self):
        newer = ['UID:u', 'DTSTAMP:20260101T000000Z', 'SEQUENCE:2',
                 'DTSTART;TZID=America/New_York:20261025T090000',
                 'RRULE:FREQ=WEEKLY;COUNT=2', 'SUMMARY:new']
        older = ['UID:u', 'DTSTAMP:20261001T000000Z', 'SEQUENCE:1',
                 'DTSTART;TZID=America/New_York:20261025T080000',
                 'RRULE:FREQ=WEEKLY;COUNT=2', 'SUMMARY:old']
        for rows in (make(newer, older), make(older, newer)):
            result = collect_occurrences(rows, window())
            self.assertEqual([item['summary'] for item in result['occurrences']],
                             ['new', 'new'])

    def test_dtstamp_breaks_sequence_ties(self):
        older = ['UID:u', 'DTSTAMP:20260101T000000Z', 'SEQUENCE:1',
                 'DTSTART;TZID=America/New_York:20261025T080000',
                 'RRULE:FREQ=WEEKLY;COUNT=2', 'SUMMARY:old']
        newer = ['UID:u', 'DTSTAMP:20261001T000000Z', 'SEQUENCE:1',
                 'DTSTART;TZID=America/New_York:20261025T090000',
                 'RRULE:FREQ=WEEKLY;COUNT=2', 'SUMMARY:new']
        for rows in (make(older, newer), make(newer, older)):
            result = collect_occurrences(rows, window())
            self.assertEqual([item['summary'] for item in result['occurrences']],
                             ['new', 'new'])
            self.assertEqual(result['quarantined'], [])

    def test_equal_versions_different_content_are_not_last_wins(self):
        first = ['UID:c', 'DTSTAMP:20261001T000000Z', 'SEQUENCE:1',
                 'DTSTART:20261101T090000Z', 'SUMMARY:甲']
        second = ['UID:c', 'DTSTAMP:20261001T000000Z', 'SEQUENCE:1',
                  'DTSTART:20261101T100000Z', 'SUMMARY:乙']
        for rows in (make(first, second), make(second, first)):
            result = collect_occurrences(rows, window())
            self.assertEqual(result['occurrences'], [])
            self.assertEqual(result['quarantined'][0]['code'], 'version_conflict')

    def test_equal_versions_identical_content_are_deduplicated(self):
        event = ['UID:c', 'DTSTAMP:20261001T000000Z', 'SEQUENCE:1',
                 'DTSTART:20261101T090000Z', 'SUMMARY:同']
        result = collect_occurrences(make(event, event), window())
        self.assertEqual(ids(result), ['c@20261101T090000'])
        self.assertEqual(result['quarantined'], [])


class RecurrenceTests(unittest.TestCase):
    def test_monthly_by_monthday_31(self):
        result = collect_occurrences(make(
            ['UID:m', 'DTSTART;VALUE=DATE:20260131',
             'RRULE:FREQ=MONTHLY;COUNT=4;BYMONTHDAY=31']),
            window('2026-01-01', '2026-12-31'))
        self.assertEqual([item['start'] for item in result['occurrences']],
                         ['2026-01-31', '2026-03-31', '2026-05-31', '2026-07-31'])

    def test_monthly_without_by_field_skips_missing_day(self):
        result = collect_occurrences(make(
            ['UID:m', 'DTSTART;VALUE=DATE:20260131',
             'RRULE:FREQ=MONTHLY;COUNT=3']),
            window('2026-01-01', '2026-12-31'))
        self.assertEqual([item['start'] for item in result['occurrences']],
                         ['2026-01-31', '2026-03-31', '2026-05-31'])
        self.assertTrue(all(item['all_day'] for item in result['occurrences']))
        # 全天活动不能机械地按 UTC 午夜换算成前一天。
        self.assertEqual(result['occurrences'][0]['start'][:10], '2026-01-31')

    def test_monthly_ordinal_byday(self):
        result = collect_occurrences(make(
            ['UID:m', 'DTSTART;VALUE=DATE:20260101',
             'RRULE:FREQ=MONTHLY;COUNT=3;BYDAY=2MO']),
            window('2026-01-01', '2026-12-31'))
        self.assertEqual([item['start'] for item in result['occurrences']],
                         ['2026-01-12', '2026-02-09', '2026-03-09'])

    def test_weekly_byday_and_interval(self):
        result = collect_occurrences(make(
            ['UID:w', 'DTSTART;TZID=America/New_York:20261102T090000',
             'DTEND;TZID=America/New_York:20261102T100000',
             'RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=MO,WE']),
            window('2026-11-01', '2026-12-31'))
        self.assertEqual([item['start'][:10] for item in result['occurrences']],
                         ['2026-11-02', '2026-11-04', '2026-11-16', '2026-11-18'])

    def test_daily_interval_and_until(self):
        result = collect_occurrences(make(
            ['UID:d', 'DTSTART;TZID=America/New_York:20261101T090000',
             'RRULE:FREQ=DAILY;INTERVAL=2;UNTIL=20261109T140000Z']), window())
        self.assertEqual([item['start'][:10] for item in result['occurrences']],
                         ['2026-11-01', '2026-11-03', '2026-11-05', '2026-11-07',
                          '2026-11-09'])

    def test_rdate_and_exdate(self):
        result = collect_occurrences(make(
            ['UID:e', 'DTSTART;TZID=America/New_York:20261026T090000',
             'DTEND;TZID=America/New_York:20261026T100000',
             'RRULE:FREQ=WEEKLY;COUNT=2',
             'EXDATE;TZID=America/New_York:20261102T090000',
             'RDATE;TZID=America/New_York:20261105T090000,20261106T090000']), window())
        self.assertEqual([item['start'][:10] for item in result['occurrences']],
                         ['2026-10-26', '2026-11-05', '2026-11-06'])

    def test_rdate_extends_beyond_rule_count(self):
        result = collect_occurrences(make(
            ['UID:e', 'DTSTART;TZID=America/New_York:20261026T090000',
             'RRULE:FREQ=WEEKLY;COUNT=1',
             'RDATE;TZID=America/New_York:20261105T090000']), window())
        self.assertEqual(len(result['occurrences']), 2)

    def test_count_is_from_series_start_not_window_start(self):
        event = ['UID:w', 'DTSTART;TZID=America/New_York:20261025T090000',
                 'RRULE:FREQ=WEEKLY;COUNT=4']
        late = collect_occurrences(make(event), window('2026-11-01', '2026-11-30'))
        self.assertEqual(ids(late), ['w@20261101T090000', 'w@20261108T090000',
                                     'w@20261115T090000'])
        full = collect_occurrences(make(event), window('2026-10-01', '2026-11-30'))
        self.assertEqual(set(ids(full)) & set(ids(late)), set(ids(late)))
        self.assertEqual([item['recurrence_key'] for item in late['occurrences']],
                         ['2026-11-01T09:00:00', '2026-11-08T09:00:00',
                          '2026-11-15T09:00:00'])

    def test_count_zero_occurrences_before_window_do_not_leak(self):
        event = ['UID:d', 'DTSTART;TZID=America/New_York:20261001T090000',
                 'RRULE:FREQ=DAILY;COUNT=3']
        result = collect_occurrences(make(event), window('2026-11-01', '2026-11-30'))
        self.assertEqual(result['occurrences'], [])

    def test_window_change_does_not_change_identity(self):
        event = ['UID:w', 'DTSTART;TZID=America/New_York:20261025T090000',
                 'RRULE:FREQ=WEEKLY;COUNT=6']
        wide = collect_occurrences(make(event), window('2026-10-01', '2026-12-31'))
        narrow = collect_occurrences(make(event), window('2026-11-02', '2026-11-30'))
        self.assertTrue(set(ids(narrow)) <= set(ids(wide)))
        for occurrence_id in ids(narrow):
            self.assertEqual(by_id(narrow, occurrence_id)['start'],
                             by_id(wide, occurrence_id)['start'])


class DaylightSavingTests(unittest.TestCase):
    def test_fall_back_keeps_local_wall_clock(self):
        result = collect_occurrences(make(
            ['UID:w', 'DTSTART;TZID=America/New_York:20261025T090000',
             'DTEND;TZID=America/New_York:20261025T100000',
             'RRULE:FREQ=WEEKLY;COUNT=3']), window())
        self.assertEqual([item['start'] for item in result['occurrences']],
                         ['2026-10-25T09:00:00-04:00',
                          '2026-11-01T09:00:00-05:00',
                          '2026-11-08T09:00:00-05:00'])
        self.assertEqual([item['end'] for item in result['occurrences']],
                         ['2026-10-25T10:00:00-04:00',
                          '2026-11-01T10:00:00-05:00',
                          '2026-11-08T10:00:00-05:00'])

    def test_spring_forward_daily(self):
        result = collect_occurrences(make(
            ['UID:s', 'DTSTART;TZID=America/New_York:20260307T090000',
             'RRULE:FREQ=DAILY;COUNT=3']), window('2026-03-01', '2026-03-31'))
        offsets = [item['start'][-6:] for item in result['occurrences']]
        self.assertEqual(offsets, ['-05:00', '-04:00', '-04:00'])

    def test_all_day_does_not_flip_date_in_other_window_zone(self):
        event = ['UID:a', 'DTSTART;VALUE=DATE:20260131',
                 'RRULE:FREQ=MONTHLY;COUNT=2']
        for zone in ('America/New_York', 'Asia/Tokyo', 'Pacific/Auckland'):
            result = collect_occurrences(make(event),
                                         window('2026-01-01', '2026-12-31', zone))
            self.assertEqual([item['start'] for item in result['occurrences']],
                             ['2026-01-31', '2026-03-31'])


class OverrideTests(unittest.TestCase):
    base = ['UID:x', 'DTSTAMP:20261001T000000Z', 'SEQUENCE:1',
            'DTSTART;TZID=America/New_York:20261025T090000',
            'DTEND;TZID=America/New_York:20261025T100000',
            'RRULE:FREQ=WEEKLY;COUNT=4']

    def test_override_moves_occurrence_and_keeps_identity(self):
        move = ['UID:x', 'DTSTAMP:20261101T000000Z', 'SEQUENCE:2',
                'RECURRENCE-ID;TZID=America/New_York:20261101T090000',
                'DTSTART;TZID=America/New_York:20261103T140000',
                'DTEND;TZID=America/New_York:20261103T150000',
                'SUMMARY:改期']
        result = collect_occurrences(make(self.base, move), window())
        moved = by_id(result, 'x@20261101T090000')
        self.assertEqual(moved['start'], '2026-11-03T14:00:00-05:00')
        self.assertTrue(moved['moved'])
        # 旧时刻不能同时保留。
        self.assertEqual([item['start'] for item in result['occurrences']],
                         ['2026-10-25T09:00:00-04:00', '2026-11-03T14:00:00-05:00',
                          '2026-11-08T09:00:00-05:00', '2026-11-15T09:00:00-05:00'])

    def test_override_inherits_duration_when_only_start_given(self):
        move = ['UID:x', 'DTSTAMP:20261101T000000Z', 'SEQUENCE:2',
                'RECURRENCE-ID;TZID=America/New_York:20261101T090000',
                'DTSTART;TZID=America/New_York:20261103T140000']
        result = collect_occurrences(make(self.base, move), window())
        self.assertEqual(by_id(result, 'x@20261101T090000')['end'],
                         '2026-11-03T15:00:00-05:00')

    def test_override_moves_into_window_from_outside(self):
        base = ['UID:y', 'DTSTAMP:20261001T000000Z', 'SEQUENCE:1',
                'DTSTART;TZID=America/New_York:20260101T090000',
                'DTEND;TZID=America/New_York:20260101T100000',
                'RRULE:FREQ=MONTHLY;COUNT=24']
        move = ['UID:y', 'DTSTAMP:20261101T000000Z', 'SEQUENCE:2',
                'RECURRENCE-ID;TZID=America/New_York:20260101T090000',
                'DTSTART;TZID=America/New_York:20261110T140000',
                'DTEND;TZID=America/New_York:20261110T150000',
                'SUMMARY:移入']
        result = collect_occurrences(make(base, move),
                                     window('2026-11-01', '2026-11-30'))
        moved = by_id(result, 'y@20260101T090000')
        self.assertEqual(moved['start'], '2026-11-10T14:00:00-05:00')
        self.assertTrue(moved['moved'])
        # 改期到窗口外时也不保留旧时刻。
        result = collect_occurrences(make(base, move),
                                     window('2026-01-01', '2026-01-31'))
        self.assertNotIn('y@20260101T090000', ids(result))

    def test_cancel_single_occurrence(self):
        cancel = ['UID:x', 'DTSTAMP:20261101T000000Z', 'SEQUENCE:2',
                  'RECURRENCE-ID;TZID=America/New_York:20261101T090000',
                  'STATUS:CANCELLED']
        result = collect_occurrences(make(self.base, cancel), window())
        self.assertNotIn('x@20261101T090000', ids(result))
        self.assertEqual(len(result['occurrences']), 3)
        self.assertEqual(result['quarantined'], [])

    def test_cancel_whole_series(self):
        cancel = ['UID:x', 'DTSTAMP:20261101T000000Z', 'SEQUENCE:2',
                  'DTSTART;TZID=America/New_York:20261025T090000',
                  'RRULE:FREQ=WEEKLY;COUNT=4', 'STATUS:CANCELLED']
        result = collect_occurrences(make(cancel), window())
        self.assertEqual(result['occurrences'], [])
        self.assertEqual(result['quarantined'], [])

    def test_missing_component_is_not_a_cancellation(self):
        result = collect_occurrences(make(self.base), window())
        self.assertEqual(len(result['occurrences']), 4)
        self.assertEqual(result['quarantined'], [])

    def test_later_cancel_does_not_be_overridden_by_earlier_move(self):
        move = ['UID:x', 'DTSTAMP:20260901T000000Z', 'SEQUENCE:1',
                'RECURRENCE-ID;TZID=America/New_York:20261101T090000',
                'DTSTART;TZID=America/New_York:20261103T140000',
                'DTEND;TZID=America/New_York:20261103T150000']
        cancel = ['UID:x', 'DTSTAMP:20261101T000000Z', 'SEQUENCE:2',
                  'RECURRENCE-ID;TZID=America/New_York:20261101T090000',
                  'STATUS:CANCELLED']
        for rows in (make(self.base, move, cancel), make(self.base, cancel, move)):
            result = collect_occurrences(rows, window())
            self.assertNotIn('x@20261101T090000', ids(result))

    def test_override_for_unknown_recurrence_still_moves_in(self):
        # 覆盖指向的起点不在生成集合里（例如父组件换了规则）时，仍按它自己的
        # 身份与时间输出，不会被静默丢弃。
        base = ['UID:z', 'DTSTAMP:20261001T000000Z', 'SEQUENCE:1',
                'DTSTART;TZID=America/New_York:20261102T090000',
                'DTEND;TZID=America/New_York:20261102T100000',
                'RRULE:FREQ=WEEKLY;COUNT=2']
        move = ['UID:z', 'DTSTAMP:20261101T000000Z', 'SEQUENCE:2',
                'RECURRENCE-ID;TZID=America/New_York:20261109T090000',
                'DTSTART;TZID=America/New_York:20261110T140000',
                'DTEND;TZID=America/New_York:20261110T150000']
        result = collect_occurrences(make(base, move), window())
        self.assertEqual(ids(result), ['z@20261102T090000', 'z@20261109T090000'])
        self.assertTrue(by_id(result, 'z@20261109T090000')['moved'])


class QuarantineTests(unittest.TestCase):
    def test_unsupported_frequency(self):
        result = collect_occurrences(make(
            ['UID:g', 'DTSTART;TZID=America/New_York:20261101T120000',
             'RRULE:FREQ=YEARLY;BYWEEKNO=5']), window())
        self.assertEqual(result['occurrences'], [])
        self.assertEqual(result['quarantined'][0]['code'], 'unsupported_expression')

    def test_unsupported_rrule_fields(self):
        for rule in ('FREQ=WEEKLY;BYMONTH=1', 'FREQ=WEEKLY;WKST=SU',
                     'FREQ=DAILY;BYHOUR=9', 'FREQ=MONTHLY;BYSETPOS=1'):
            result = collect_occurrences(make(
                ['UID:g', 'DTSTART;TZID=America/New_York:20261101T120000',
                 f'RRULE:{rule}']), window())
            self.assertEqual(result['occurrences'], [])
            self.assertEqual(result['quarantined'][0]['code'], 'unsupported_expression')

    def test_unknown_timezone(self):
        result = collect_occurrences(make(
            ['UID:g', 'DTSTART;TZID=Custom/Unknown:20261101T120000',
             'RRULE:FREQ=DAILY;COUNT=2']), window())
        self.assertEqual(result['quarantined'][0]['code'], 'unknown_timezone')

    def test_quarantine_does_not_affect_other_uids(self):
        good = ['UID:ok', 'DTSTART;TZID=America/New_York:20261101T090000',
                'RRULE:FREQ=DAILY;COUNT=2']
        bad = ['UID:bad', 'DTSTART;TZID=America/New_York:20261101T090000',
               'RRULE:FREQ=HOURLY;COUNT=2']
        result = collect_occurrences(make(good, bad, good), window())
        self.assertEqual(len(result['occurrences']), 2)
        self.assertEqual([item['uid'] for item in result['quarantined']], ['bad'])

    def test_single_event_without_rule_is_one_occurrence(self):
        result = collect_occurrences(make(
            ['UID:one', 'DTSTART;TZID=America/New_York:20261120T090000',
             'DTEND;TZID=America/New_York:20261120T100000']), window())
        self.assertEqual(ids(result), ['one@20261120T090000'])
        self.assertEqual(result['quarantined'], [])


class DistinctnessTests(unittest.TestCase):
    def test_two_events_at_same_instant_stay_separate(self):
        result = collect_occurrences(make(
            ['UID:a', 'DTSTART;TZID=America/New_York:20261101T090000',
             'DTEND;TZID=America/New_York:20261101T100000'],
            ['UID:b', 'DTSTART;TZID=America/New_York:20261101T090000',
             'DTEND;TZID=America/New_York:20261101T100000']), window())
        self.assertEqual(ids(result), ['a@20261101T090000', 'b@20261101T090000'])
        self.assertEqual(result['occurrences'][0]['start'], result['occurrences'][1]['start'])

    def test_same_uid_same_time_is_one_occurrence(self):
        event = ['UID:a', 'DTSTART;TZID=America/New_York:20261101T090000',
                 'RRULE:FREQ=WEEKLY;COUNT=2',
                 'RDATE;TZID=America/New_York:20261101T090000']
        result = collect_occurrences(make(event), window())
        self.assertEqual(ids(result), ['a@20261101T090000', 'a@20261108T090000'])

    def test_ids_are_unique_across_window_and_rerun(self):
        _, rows = read('fixtures/calendar.ics')
        result = collect_occurrences(rows, window())
        self.assertEqual(len(ids(result)), len(set(ids(result))))

    def test_output_is_json_serialisable_and_finite(self):
        import json
        _, rows = read('fixtures/calendar.ics')
        result = collect_occurrences(rows, window())
        json.dumps(result, ensure_ascii=False, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
