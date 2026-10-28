import unittest

from worklog.adapters import event
from worklog.core import (
    bounds,
    count,
    redact_title,
    summarize,
    timestamp,
    union_seconds,
    usage,
)

KEY = b'x' * 32
DAY = '2026-01-02'
START, END = bounds(DAY, 'UTC')


def row(index, seconds=0, session='one', kind='assistant', **kw):
    return event(KEY, 'pi', session, '/private/project', str(index), START + seconds, kind,
                 'codebuddy', 'test-model', **kw)


class CoreTests(unittest.TestCase):
    def test_counters_reject_bad_numbers(self):
        for value in [None, True, '100', -1, float('nan'), float('inf')]:
            self.assertEqual(count(value), 0)
        self.assertEqual(count(12.9), 12)
        self.assertEqual(count(10**15), 10**12)

    def test_timestamp_normalization(self):
        self.assertEqual(timestamp('2026-01-02T08:00:00+08:00'), START)
        self.assertEqual(timestamp(START * 1000), START)
        self.assertIsNone(timestamp('2026-01-02T00:00:00'))
        self.assertIsNone(timestamp(True))

    def test_day_boundary_and_dst(self):
        start, end = bounds('2026-03-08', 'America/New_York')
        self.assertEqual(end - start, 23 * 3600)
        with self.assertRaises(ValueError):
            bounds('2026-02-30', 'UTC')

    def test_inclusive_codex_cache_and_reasoning_are_not_double_counted(self):
        value = usage({'input_tokens': 1000, 'cached_input_tokens': 800,
                       'cache_write_input_tokens': 50, 'output_tokens': 100,
                       'reasoning_output_tokens': 70, 'total_tokens': 1100}, inclusive=True)
        self.assertEqual(value['input'], 150)
        self.assertEqual(value['tokens'], 1100)

    def test_pi_usage_has_exclusive_input_and_cost_zero_is_not_missing(self):
        value = usage({'input': 100, 'cacheRead': 900, 'output': 20,
                       'reasoning': 10, 'cost': {'total': 0}})
        self.assertEqual(value['tokens'], 1020)
        self.assertEqual(value['cost'], 0)
        self.assertIsNone(usage({})['cost'])

    def test_bad_cache_counts_do_not_inflate_input(self):
        value = usage({'input_tokens': 10, 'cached_input_tokens': 999,
                       'cache_write_input_tokens': 999}, inclusive=True)
        self.assertEqual(value['tokens'], 10)
        self.assertEqual(value['input'], 0)

    def test_overlapping_intervals_are_a_union(self):
        self.assertEqual(union_seconds([(0, 100), (50, 150), (150, 180), (200, 210)]), 190)

    def test_parallel_sessions_do_not_double_active_time(self):
        value = summarize([row(1, 0), row(2, 100), row(3, 50, 'two'), row(4, 150, 'two')], DAY, 'UTC')
        self.assertEqual(value['estimated_active_seconds'], 150)
        self.assertEqual(value['request_count'], 4)

    def test_idle_gaps_and_single_events_do_not_invent_time(self):
        value = summarize([row(1, 0), row(2, 600)], DAY, 'UTC')
        self.assertEqual(value['estimated_active_seconds'], 0)
        self.assertEqual(summarize([row(1)], DAY, 'UTC')['estimated_active_seconds'], 0)

    def test_cross_midnight_intervals_are_clipped(self):
        value = summarize([row(1, -60), row(2, 60), row(3, 86340), row(4, 86460)], DAY, 'UTC')
        self.assertEqual(value['estimated_active_seconds'], 120)
        self.assertEqual(value['event_count'], 2)

    def test_auxiliary_usage_does_not_make_an_activity_interval(self):
        value = summarize([row(1, 0, kind='auxiliary'), row(2, 100)], DAY, 'UTC')
        self.assertEqual(value['estimated_active_seconds'], 0)
        self.assertEqual(value['request_count'], 1)

    def test_titles_are_redacted_and_bounded(self):
        sample = '修复 token=not-a-real-token user@example.com 13800138000 https://example.com/?secret=x /Users/test/private'
        output = redact_title(sample)
        for secret in ['not-a-real-token', 'user@example.com', '13800138000', 'example.com', '/Users/']:
            self.assertNotIn(secret, output)
        self.assertLessEqual(len(redact_title('中' * 1000)), 100)

    def test_error_status_not_claimed_as_completed_work(self):
        value = summarize([row(1, status='error')], DAY, 'UTC')
        self.assertEqual(value['error_count'], 1)
        self.assertTrue(any('不等于任务完成' in x for x in value['limitations']))


if __name__ == '__main__':
    unittest.main()
