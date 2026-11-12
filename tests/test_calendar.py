import unittest
from calendar_source import components, read, unfold


class CalendarTests(unittest.TestCase):
    def test_components_are_raw(self):
        raw, rows = read('fixtures/calendar.ics')
        self.assertEqual(len(rows), 3)
        self.assertIn(b'RRULE', raw)

    def test_unfold(self):
        self.assertEqual(unfold(b'A:one\r\n two\r\n'), ['A:onetwo'])

    def test_bad_fold(self):
        with self.assertRaises(ValueError):
            unfold(b' no-parent')

    def test_truncated(self):
        with self.assertRaises(ValueError):
            components(b'BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VCALENDAR')

    def test_property_parameters_preserved(self):
        _, rows = read('fixtures/calendar.ics')
        self.assertIn({'property': 'DTSTART;TZID=America/New_York', 'value': '20261025T090000'}, rows[0])
