from datetime import date, datetime
from unittest import TestCase

from ..provenance import (
    Declaration,
    LayeredCalendar,
    ResolutionStatus,
    Source,
    SourceLevel,
    explain,
    resolve,
)

SATURDAY = date(2026, 5, 9)
FRIDAY = date(2026, 5, 8)


class BaseCalendar:
    name = 'group-base-calendar'

    def is_working_day(self, day):
        return day.weekday() < 5


class DeclarationTest(TestCase):

    def test_normalization(self):
        declaration = Declaration(
            '2026-05-09', working=True, source='group',
            level='branch', published_at='2026-05-01T09:00:00',
        )
        self.assertEqual(declaration.day, SATURDAY)
        self.assertEqual(declaration.level, SourceLevel.BRANCH)
        self.assertTrue(declaration.is_working_day)
        self.assertEqual(declaration.status, 'working')
        self.assertEqual(declaration.published_at,
                         datetime(2026, 5, 1, 9, 0))
        self.assertEqual(declaration.published_at.tzinfo, None)

    def test_is_working_day_alias(self):
        declaration = Declaration(SATURDAY, is_working_day=False)
        self.assertFalse(declaration.working)
        self.assertEqual(declaration.status, 'not_working')

    def test_conflicting_aliases_are_rejected(self):
        with self.assertRaises(ValueError):
            Declaration(SATURDAY, working=True, is_working_day=False)

    def test_withdrawal_before_publication_is_rejected(self):
        with self.assertRaises(ValueError):
            Declaration(SATURDAY, True,
                        published_at='2026-05-01',
                        withdrawn_at='2026-04-01')

    def test_source_object(self):
        declaration = Declaration(
            SATURDAY, True, source=Source('branch', level='branch'))
        self.assertEqual(declaration.source, 'branch')
        self.assertEqual(declaration.level, SourceLevel.BRANCH)

    def test_level_aliases(self):
        cases = {
            'company': SourceLevel.GROUP,
            '集团': SourceLevel.GROUP,
            '分公司': SourceLevel.BRANCH,
            'individual': SourceLevel.PERSONAL,
        }
        for value, expected in cases.items():
            declaration = Declaration(SATURDAY, True,
                                      source='test', level=value)
            self.assertEqual(declaration.level, expected)

    def test_as_dict_and_id(self):
        declaration = Declaration(
            SATURDAY, True, source='branch', level='branch',
            notice_id='N-42', label='补班')
        self.assertEqual(declaration.id, 'branch:branch:2026-05-09:v1')
        data = declaration.as_dict()
        self.assertEqual(data['notice_id'], 'N-42')
        self.assertEqual(data['label'], '补班')
        self.assertEqual(data['level'], 'branch')


class LayeredResolutionTest(TestCase):

    def setUp(self):
        self.day = SATURDAY
        self.calendar = LayeredCalendar()
        self.group = self.calendar.declare(
            self.day, True, source='group-calendar', level='group',
            published_at='2026-04-20', label='补班', rule='group-calendar',
        )
        self.branch = self.calendar.declare(
            self.day, False, source='branch-shanghai', level='branch',
            published_at='2026-05-07', label='临时停工',
            notice_id='B-1', rule='temporary-stoppage',
        )
        self.personal = None

    def add_personal(self):
        self.personal = self.calendar.declare(
            self.day, False, source='employee', level='personal',
            published_at='2026-05-08', label='个人休假',
            notice_id='P-1',
        )
        return self.personal

    def test_branch_stoppage_wins_over_group_makeup(self):
        result = self.calendar.resolve(self.day)
        self.assertTrue(result.is_resolved)
        self.assertFalse(result.working)
        self.assertEqual(result.winner, self.branch)
        self.assertEqual([item.source for item in result.shadowed],
                         ['group-calendar'])
        self.assertIn('overridden by higher-priority source',
                      result.reason_for(self.group))

    def test_personal_wins_over_branch(self):
        personal = self.add_personal()
        result = self.calendar.resolve(self.day)
        self.assertFalse(result.working)
        self.assertEqual(result.winner, personal)
        self.assertEqual(result.winner.level, SourceLevel.PERSONAL)
        self.assertEqual(
            {item.source for item in result.shadowed},
            {'group-calendar', 'branch-shanghai'},
        )

    def test_same_level_conflict_stays_pending(self):
        self.calendar.declare(
            self.day, True, source='branch-beijing', level='branch',
            published_at='2026-05-07', notice_id='B-2',
        )
        result = self.calendar.resolve(self.day)
        self.assertEqual(result.status, ResolutionStatus.PENDING)
        self.assertIsNone(result.working)
        self.assertIsNone(result.winner)
        self.assertEqual(len(result.conflicts), 2)
        self.assertEqual(
            {item.source for item in result.conflicts},
            {'branch-shanghai', 'branch-beijing'},
        )
        self.assertIn(self.group, result.shadowed)
        self.assertTrue(any('higher-priority' in reason
                            for reason in result.shadow_reasons))

    def test_withdrawn_old_notice_is_ignored_and_explained(self):
        personal = self.add_personal()
        self.calendar.withdraw('P-1', at='2026-05-08T09:00:00')
        result = self.calendar.resolve(self.day, as_of='2026-05-08T12:00:00')
        self.assertFalse(result.working)
        self.assertEqual(result.winner, self.branch)
        self.assertIn(personal.withdrawn_at, (None,))
        self.assertIn(
            personal.id,
            [item.id for item in result.withdrawn],
        )
        self.assertNotIn(personal.id,
                         [item.id for item in result.active])
        self.assertEqual(result.winner, self.branch)

    def test_withdrawing_stoppage_reveals_group_makeup(self):
        self.add_personal()
        self.calendar.withdraw('B-1', at='2026-05-08T18:00:00')
        self.calendar.withdraw('P-1', at='2026-05-08T09:00:00')
        result = self.calendar.resolve(self.day, as_of='2026-05-08T20:00:00')
        self.assertTrue(result.working)
        self.assertEqual(result.winner, self.group)
        self.assertEqual(result.shadowed, ())
        self.assertEqual(
            {item.notice_id for item in result.withdrawn},
            {'B-1', 'P-1'},
        )

    def test_query_before_withdrawal_still_sees_stoppage(self):
        self.calendar.withdraw('B-1', at='2026-05-08T18:00:00')
        result = self.calendar.resolve(self.day, as_of='2026-05-08T12:00:00')
        self.assertFalse(result.working)
        self.assertEqual(result.winner.id, self.branch.id)
        self.assertEqual(result.withdrawn, ())

    def test_notification_published_after_query_is_hidden(self):
        later = self.calendar.declare(
            self.day, True, source='late-notice', level='personal',
            published_at='2026-05-10', notice_id='L-1',
        )
        result = self.calendar.resolve(self.day, as_of='2026-05-09')
        self.assertEqual(result.status, ResolutionStatus.RESOLVED)
        self.assertEqual(result.winner, self.branch)
        self.assertIn(later, result.not_published)
        self.assertEqual(result.not_published[0].published_at,
                         datetime(2026, 5, 10, 0, 0))

    def test_latest_version_of_same_source_wins(self):
        calendar = LayeredCalendar()
        calendar.declare(
            self.day, True, source='branch-shanghai', level='branch',
            version=1, published_at='2026-05-01')
        calendar.declare(
            self.day, False, source='branch-shanghai', level='branch',
            version=2, published_at='2026-05-03')
        first = calendar.resolve(self.day, as_of='2026-05-02')
        second = calendar.resolve(self.day, as_of='2026-05-04')
        self.assertTrue(first.working)
        self.assertEqual(first.winner.version, 1)
        self.assertFalse(second.working)
        self.assertEqual(second.winner.version, 2)
        self.assertEqual([item.version for item in second.shadowed], [1])

    def test_unpublished_version_does_not_override_visible_one(self):
        calendar = LayeredCalendar()
        calendar.declare(
            self.day, True, source='branch', level='branch',
            version=1, published_at='2026-05-01')
        calendar.declare(
            self.day, False, source='branch', level='branch',
            version=2, published_at='2026-05-10')
        result = calendar.resolve(self.day, as_of='2026-05-05')
        self.assertTrue(result.working)
        self.assertEqual(result.winner.version, 1)
        self.assertEqual(
            {item.version for item in result.not_published}, {2})

    def test_no_data_without_base_calendar(self):
        result = LayeredCalendar().resolve(FRIDAY)
        self.assertEqual(result.status, ResolutionStatus.NO_DATA)
        self.assertIsNone(result.working)
        self.assertEqual(result.winner, None)
        self.assertEqual(result.shadowed, ())

    def test_base_calendar_is_visible_and_shadowed(self):
        calendar = LayeredCalendar(base_calendar=BaseCalendar())
        default = calendar.resolve(SATURDAY)
        self.assertEqual(default.status, ResolutionStatus.RESOLVED)
        self.assertFalse(default.working)
        self.assertEqual(default.winner.source, 'group-base-calendar')

        branch = calendar.declare(
            SATURDAY, True, source='branch', level='branch',
            published_at='2026-05-01', notice_id='B-9',
        )
        result = calendar.resolve(SATURDAY)
        self.assertTrue(result.working)
        self.assertEqual(result.winner, branch)
        self.assertEqual([item.source for item in result.shadowed],
                         ['group-base-calendar'])
        calendar.withdraw('B-9', at='2026-05-01T12:00:00')
        restored = calendar.resolve(SATURDAY, as_of='2026-05-01T13:00:00')
        self.assertEqual(restored.winner.source, 'group-base-calendar')

    def test_explanation_always_lists_winner_and_shadowed(self):
        result = self.calendar.resolve(self.day)
        text = result.explain()
        self.assertIn('winner: branch-shanghai', text)
        self.assertIn('shadowed:', text)
        self.assertIn('group-calendar', text)
        data = result.to_dict()
        self.assertEqual(data['winner']['source'], 'branch-shanghai')
        self.assertEqual(data['shadowed'][0]['declaration']['source'],
                         'group-calendar')
        self.assertTrue(data['shadowed'][0]['reason'])

    def test_pending_explanation_lists_no_winner(self):
        self.calendar.declare(
            self.day, True, source='branch-beijing', level='branch')
        text = self.calendar.resolve(self.day).explain()
        self.assertIn('winner: none', text)
        self.assertIn('pending same-level conflict:', text)

    def test_query_aliases(self):
        result = self.calendar.query(business_day=self.day,
                                     notification_time='2026-05-08')
        self.assertFalse(result.working)
        self.assertEqual(result.as_of,
                         datetime(2026, 5, 8, 23, 59, 59, 999999))

    def test_is_working_day_pending_is_none(self):
        calendar = LayeredCalendar()
        calendar.declare(self.day, True, source='a', level='branch')
        calendar.declare(self.day, False, source='b', level='branch')
        self.assertIsNone(calendar.is_working_day(self.day))
        self.assertIsNone(calendar.is_holiday(self.day))

    def test_withdraw_unknown_declaration(self):
        with self.assertRaises(KeyError):
            self.calendar.withdraw('missing')

    def test_module_level_helpers(self):
        result = resolve(self.calendar.declarations, self.day,
                         as_of='2026-05-08T12:00:00')
        self.assertFalse(result.working)
        text = explain(self.calendar.declarations, self.day,
                       as_of='2026-05-08T12:00:00')
        self.assertIn('winner:', text)

    def test_midnight_withdrawal_is_effective_for_that_day(self):
        self.calendar.withdraw('B-1', at='2026-05-07T23:00:00')
        result = self.calendar.resolve(self.day, as_of='2026-05-07')
        self.assertIn(self.branch.id,
                      [item.id for item in result.withdrawn])


class ChinaCalendarIntegrationTest(TestCase):
    """The group calendar's Saturday make-up must survive a branch stoppage."""

    def setUp(self):
        import warnings
        from ..asia import China
        warnings.simplefilter('ignore')
        self.day = date(2023, 10, 7)  # SAT, a make-up workday in China
        self.calendar = LayeredCalendar(base_calendar=China())

    def test_group_makeup_is_the_default(self):
        result = self.calendar.resolve(self.day)
        self.assertTrue(result.working)
        self.assertEqual(result.winner.source, 'China')

    def test_branch_stoppage_then_withdrawal(self):
        self.calendar.declare(
            self.day, False, source='branch-shanghai', level='branch',
            published_at='2023-10-05', notice_id='B-2023-10-05',
            label='临时停工',
        )
        stopped = self.calendar.resolve(self.day)
        self.assertFalse(stopped.working)
        self.assertEqual(stopped.winner.source, 'branch-shanghai')
        self.assertEqual([item.source for item in stopped.shadowed], ['China'])

        self.calendar.withdraw('B-2023-10-05', at='2023-10-06T18:00:00')
        restored = self.calendar.resolve(
            self.day, as_of='2023-10-06T19:00:00')
        self.assertTrue(restored.working)
        self.assertEqual(restored.winner.source, 'China')

    def test_collected_dict_inputs(self):
        calendar = LayeredCalendar([
            {
                'date': '2023-10-07',
                'status': 'working',
                'source': 'group-calendar',
                'source_level': 'group',
                'published_time': '2023-09-20',
            },
            {
                'business_day': '2023-10-07',
                'status': 'not_working',
                'source': 'branch-shanghai',
                'source_layer': 'branch',
                'published': '2023-10-05',
                'notice_id': 'B-1',
            },
        ])
        result = calendar.resolve(self.day, as_of='2023-10-06')
        self.assertFalse(result.working)
        self.assertEqual(result.winner.notice_id, 'B-1')
        self.assertEqual([item.source for item in result.shadowed],
                         ['group-calendar'])


class RegionAndStatusTest(TestCase):

    def test_region_constructor(self):
        import warnings
        warnings.simplefilter('ignore')
        calendar = LayeredCalendar(region='CN')
        self.assertEqual(calendar.resolve(date(2023, 10, 7)).working, True)

    def test_unknown_region(self):
        with self.assertRaises(ValueError):
            LayeredCalendar(region='ZZ')

    def test_region_and_base_calendar_are_exclusive(self):
        with self.assertRaises(TypeError):
            LayeredCalendar(region='CN', base_calendar=BaseCalendar())

    def test_status_field_and_rule_alias(self):
        from ..provenance import Rule
        declaration = Rule(
            SATURDAY, status='closed', source='branch', level='branch')
        self.assertFalse(declaration.working)
        self.assertFalse(declaration.is_working_day)

    def test_utc_timestamp_is_normalized(self):
        declaration = Declaration(
            SATURDAY, True, published_at='2026-05-08T20:00:00Z')
        self.assertEqual(declaration.published_at,
                         datetime(2026, 5, 8, 20, 0))
        self.assertEqual(declaration.published_at.tzinfo, None)
