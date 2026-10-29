import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fleet import summarize
from live_tracker import LiveTracker
from manual_actions import candidate
from monitor import Monitor
from thunder_api import read_task_archive


def native_task(**changes):
    return dict(dict(taskId=20, taskName='archived magnet', taskStatus=7,
                     fileSize=100, downloadSize=20, savePath='D:/downloads',
                     url='magnet:?xt=urn:btih:'+'a'*40, recycleTime=1792566875968,
                     createTime=1790000000000, completionTime=0), **changes)


def task(task_id=1, **changes):
    return dict(dict(id=task_id, name='test', status=5, active=True, speed=0,
                     received=10, size=100, save_path='D:/downloads'), **changes)


class ArchiveTests(unittest.TestCase):
    def test_recycle_fields_and_read_only_even_when_original_status_is_downloading(self):
        with patch('thunder_api.remote', return_value={'tasks': [native_task(taskStatus=5)]}) as call:
            rows = read_task_archive({'id': 'pc'}, 'recycle', Path('/tenant'))
        row = rows[0]
        self.assertEqual(row['view'], 'recycle')
        self.assertTrue(row['read_only'])
        self.assertFalse(row['active'])
        self.assertFalse(row['delete_eligible'])
        self.assertEqual(row['recycle_time'], 1792566875968)
        self.assertTrue(row['url'].startswith('magnet:'))
        self.assertEqual(call.call_args.args[2], Path('/tenant'))
        with self.assertRaises(ValueError):
            read_task_archive({}, 'delete')

    def test_archive_validation_rejects_duplicates_and_invalid_numeric_values(self):
        for response in ({'tasks': None}, {'tasks': [native_task(), native_task()]},
                         {'tasks': [native_task(taskId=True)]},
                         {'tasks': [native_task(recycleTime=-1)]}):
            with patch('thunder_api.remote', return_value=response):
                with self.assertRaises(ValueError):
                    read_task_archive({}, 'recycle')

    def test_recycle_and_completed_are_never_zero_speed_candidates(self):
        for row in (task(view='recycle'), task(read_only=True), task(recycle_time=100), task(view='completed')):
            self.assertFalse(candidate(row))
            tracker = LiveTracker(10, 30)
            tracker.update([row], 0)
            self.assertFalse(tracker.update([row], 20)[0]['delete_eligible'])

    def test_archives_never_enter_automatic_cleanup_or_acceleration_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            host = dict(id='pc', address='example', credentials_file='password.txt', auto_enabled=True)
            monitor = Monitor(dict(poll_seconds=5, idle_seconds=10, hosts=[host]), data_dir=Path(directory))
            recycled = task(2, view='recycle', read_only=True, recycle_time=100)
            live = task(1, view='incomplete')
            remove = Mock(return_value=dict(deleted=True, filesDeleted=False))
            with patch('monitor.read_live_tasks', return_value=[live, recycled]), patch('monitor.read_task_archive', return_value=[recycled]), patch('monitor.remove_keep_files', remove):
                with patch('monitor.time.monotonic', return_value=0):
                    monitor.poll_host(host)
                with patch('monitor.time.monotonic', return_value=10):
                    monitor.poll_host(host)
            remove.assert_called_once()
            self.assertEqual(remove.call_args.args[1]['id'], 1)
            self.assertNotIn(2, monitor.trackers['pc'].previous)
            self.assertNotIn(('pc', 2), monitor.uncertain)
            self.assertEqual(monitor.snapshot()['hosts'][0]['archives']['recycle']['tasks'][0]['id'], 2)
            monitor.hosts['pc']['checked_at'] = 0  # Exercise a fresh remote preview rather than the post-delete cache.
            with patch('manual_actions.read_live_tasks', return_value=[live, recycled]):
                preview = monitor.actions.prepare('pc', 'del_zero')
                self.assertEqual([row['id'] for row in preview['tasks']], [1])
                for command in ('del', 'delf'):
                    with self.assertRaises(ValueError):
                        monitor.actions.prepare('pc', command, 2)

    def test_cache_refresh_failure_retains_records_and_does_not_break_live_monitor(self):
        with tempfile.TemporaryDirectory() as directory:
            host = dict(id='pc', address='example')
            monitor = Monitor(dict(poll_seconds=5, idle_seconds=10, hosts=[host]), data_dir=Path(directory))
            archived = task(2, view='recycle', read_only=True)
            with patch('monitor.read_task_archive', return_value=[archived]) as read:
                with patch('monitor.time.monotonic', return_value=0):
                    monitor.refresh_archives(host)
                with patch('monitor.time.monotonic', return_value=59):
                    monitor.refresh_archives(host)
                self.assertEqual(read.call_count, 2)
            with patch('monitor.read_live_tasks', return_value=[task()]), patch('monitor.read_task_archive', side_effect=TimeoutError()):
                with patch('monitor.time.monotonic', return_value=60):
                    monitor.poll_host(host)
            snap = monitor.snapshot()['hosts'][0]
            self.assertEqual(snap['state'], '在线')
            self.assertEqual(snap['archives']['recycle']['tasks'][0]['id'], 2)
            self.assertEqual(snap['archives']['recycle']['state'], 'error')
            self.assertIsNone(summarize(snap, snap['checked_at'], 60)['recycle'])

    def test_dashboard_counts_archives_separately_from_downloads_and_speed(self):
        host = dict(id='pc', state='在线', checked_at=100, tasks=[task(speed=100)],
                    archives=dict(recycle=dict(state='ready', checked_at=100, tasks=[task(2, speed=999999)]),
                                  completed=dict(state='ready', checked_at=100, tasks=[task(3)])))
        stats = summarize(host, 101, 60)
        self.assertEqual((stats['tasks'], stats['downloading'], stats['speed'], stats['recycle'], stats['completed']), (1, 1, 100, 1, 1))
        self.assertIsNone(summarize(host, 300, 60)['recycle'])
