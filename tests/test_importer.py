import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from imdb_plex_cloud import cli
from imdb_plex_cloud.core import (ImportProblem, apply_plan, make_plan, private_dir,
    read_csv, read_json, restore_backup, validate_plan, write_json)
from imdb_plex_cloud.plex import METADATA, NoRedirect, PlexCloud


KEY = 'a' * 24
SECOND_KEY = 'b' * 24


class FakePlex:
    account_id = 'test-account'

    def __init__(self, current=None):
        self.ratings = {KEY: current, SECOND_KEY: None}
        self.writes = []
        self.fail_write = False

    def match(self, imdb_id, media_type):
        return {'status': 'matched', 'key': KEY if imdb_id == 'tt1' else SECOND_KEY,
                'plex_title': 'Synthetic title', 'plex_year': 2000}

    def get_rating(self, key):
        return self.ratings[key]

    def set_rating(self, key, score):
        if self.fail_write:
            raise ImportProblem('Synthetic failure')
        self.writes.append((key, score))
        self.ratings[key] = score


def sample_rows():
    return [{'imdb_id': 'tt1', 'title': 'Synthetic title', 'year': '2000',
             'rating': 8, 'media_type': 'movie', 'csv_line': 2}]


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = FakePlex(3)
        self.plan = make_plan(self.client, sample_rows(), [])
        write_json(self.root / 'plan.json', self.plan)

    def csv(self, text):
        path = self.root / 'input.csv'
        path.write_text(text, encoding='utf-8-sig')
        return read_csv(path)

    def test_csv_bom_and_unicode(self):
        rows, excluded = self.csv('Const,Your Rating,Title,Title Type,Year\ntt1,8,Café,Movie,2000\n')
        self.assertEqual(rows[0]['title'], 'Café')
        self.assertEqual(excluded, [])

    def test_upstream_unmatched_format(self):
        rows, _ = self.csv('IMDbID,YourRating,Title,TitleType\ntt2,7,Example,TV Series\n')
        self.assertEqual(rows[0]['media_type'], 'show')

    def test_bad_scores_and_unsupported_types(self):
        text = 'Const,Your Rating,Title,Title Type\n'
        text += ''.join(f'tt{i},'+score+',Example,Movie\n' for i, score in enumerate(['nan', 'inf', '-1', '0', '11', ''], 1))
        text += 'tt10,9,Episode,TV Episode\ntt11,9,Game,Video Game\n'
        rows, excluded = self.csv(text)
        self.assertFalse(rows)
        self.assertEqual(len(excluded), 8)

    def test_duplicate_and_conflicting_ratings(self):
        rows, excluded = self.csv('Const,Your Rating,Title,Title Type\ntt1,8,A,Movie\ntt1,8,A,Movie\ntt2,7,B,Movie\ntt2,9,B,Movie\n')
        self.assertEqual(len(rows), 1)
        self.assertEqual([e['status'] for e in excluded].count('conflicting_duplicates'), 2)

    def test_wrong_csv_headers(self):
        with self.assertRaises(ImportProblem):
            self.csv('Title,Rating\nA,8\n')

    def test_plan_is_read_only_and_account_bound(self):
        self.assertEqual(self.client.writes, [])
        with self.assertRaises(ImportProblem):
            apply_plan(FakeOtherAccount(), self.plan, self.root)
        self.plan['items'][0]['rating'] = 10
        with self.assertRaises(ImportProblem):
            validate_plan(self.plan)

    def test_lookup_failure_is_excluded(self):
        with patch.object(self.client, 'match', side_effect=ImportProblem('offline')):
            plan = make_plan(self.client, sample_rows(), [])
        self.assertEqual(plan['items'][0]['status'], 'lookup_failed')
        self.assertFalse(apply_plan(self.client, plan, self.root)['completed'])
        self.assertFalse(self.client.writes)

    def test_backup_exists_before_write_and_resume_skips_completed(self):
        original = self.client.set_rating
        def checked_write(key, score):
            backup = read_json(self.root / 'backup.json')
            self.assertEqual(backup['items']['tt1']['previous_rating'], 3)
            original(key, score)
        with patch.object(self.client, 'set_rating', side_effect=checked_write):
            apply_plan(self.client, self.plan, self.root)
            apply_plan(self.client, self.plan, self.root)
        self.assertEqual(self.client.writes, [(KEY, 8)])

    def test_backup_write_failure_blocks_plex_write(self):
        with patch('imdb_plex_cloud.core.write_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                apply_plan(self.client, self.plan, self.root)
        self.assertFalse(self.client.writes)

    def test_unchanged_score_has_no_write(self):
        self.client.ratings[KEY] = 8
        state = apply_plan(self.client, self.plan, self.root)
        self.assertEqual(state['completed']['tt1'], 'unchanged')
        self.assertFalse(self.client.writes)

    def test_failed_write_retries_and_preserves_original_backup(self):
        self.client.fail_write = True
        state = apply_plan(self.client, self.plan, self.root)
        self.assertIn('tt1', state['failed'])
        self.client.fail_write = False
        apply_plan(self.client, self.plan, self.root)
        self.assertEqual(read_json(self.root / 'backup.json')['items']['tt1']['previous_rating'], 3)
        self.assertEqual(self.client.ratings[KEY], 8)

    def test_uncertain_success_resumes_without_duplicate_write(self):
        reads = [3, ImportProblem('verification interrupted')]
        with patch.object(self.client, 'get_rating', side_effect=reads):
            self.assertIn('tt1', apply_plan(self.client, self.plan, self.root)['failed'])
        result = apply_plan(self.client, self.plan, self.root)
        self.assertEqual(result['completed']['tt1'], 'updated')
        self.assertEqual(len(self.client.writes), 1)

    def test_missing_backup_is_detected(self):
        apply_plan(self.client, self.plan, self.root)
        (self.root / 'backup.json').unlink()
        with self.assertRaises(ImportProblem):
            apply_plan(self.client, self.plan, self.root)

    def test_verification_mismatch_is_not_marked_completed(self):
        with patch.object(self.client, 'set_rating'):
            state = apply_plan(self.client, self.plan, self.root)
        self.assertIn('tt1', state['failed'])
        self.assertNotIn('tt1', state['completed'])

    def test_three_consecutive_failures_stop_before_remaining_titles(self):
        rows = [dict(sample_rows()[0], imdb_id=f'tt{i}') for i in range(1, 6)]
        plan = make_plan(self.client, rows, [])
        self.client.fail_write = True
        with self.assertRaises(ImportProblem):
            apply_plan(self.client, plan, self.root)
        self.assertEqual(len(read_json(self.root / 'checkpoint.json')['failed']), 3)

    def test_restore_refuses_another_account(self):
        apply_plan(self.client, self.plan, self.root)
        other = FakeOtherAccount()
        with self.assertRaises(ImportProblem):
            restore_backup(other, self.root)
        self.assertEqual(other.writes, [])

    def test_restore_previous_score_and_retry(self):
        apply_plan(self.client, self.plan, self.root)
        report = restore_backup(self.client, self.root)
        self.assertEqual(report['restored'], ['tt1'])
        self.assertEqual(self.client.ratings[KEY], 3)
        self.assertEqual(restore_backup(self.client, self.root)['already_restored'], ['tt1'])

    def test_restore_clears_previously_unrated(self):
        self.client.ratings[KEY] = None
        apply_plan(self.client, self.plan, self.root)
        restore_backup(self.client, self.root)
        self.assertIsNone(self.client.ratings[KEY])

    def test_restore_preserves_later_edits(self):
        apply_plan(self.client, self.plan, self.root)
        self.client.ratings[KEY] = 9
        self.assertEqual(restore_backup(self.client, self.root)['conflicts'], ['tt1'])
        self.assertEqual(self.client.ratings[KEY], 9)

    def test_dry_run_never_authenticates(self):
        with patch('imdb_plex_cloud.cli.sign_in') as auth, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['apply', '--state-dir', str(self.root), '--dry-run']), 0)
        auth.assert_not_called()
        self.assertFalse((self.root / 'checkpoint.json').exists())

    def test_malformed_state_is_safe_error(self):
        (self.root / 'checkpoint.json').write_text('[]')
        with self.assertRaises(ImportProblem):
            apply_plan(self.client, self.plan, self.root)

    @unittest.skipIf(os.name == 'nt', 'POSIX file permissions')
    def test_private_file_permissions_and_symlink_rejection(self):
        folder = private_dir(self.root / 'state')
        write_json(folder / 'test.json', {'synthetic': True})
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual((folder / 'test.json').stat().st_mode & 0o777, 0o600)
        link = self.root / 'link'
        link.symlink_to(folder, target_is_directory=True)
        with self.assertRaises(ImportProblem):
            private_dir(link)


class FakeOtherAccount(FakePlex):
    account_id = 'different-test-account'


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return Response(value)


def metadata(**extra):
    return {'MediaContainer': {'Metadata': [dict(ratingKey=KEY, type='movie', title='Synthetic', Guid=[{'id': 'imdb://tt1'}], **extra)]}}


class TransportTests(unittest.TestCase):
    def client(self, *responses):
        opener = Opener([{'id': 'test-account'}, *responses])
        client = PlexCloud('synthetic-test-token', opener=opener, sleeper=lambda _: None)
        return client, opener

    def test_exact_match_and_reject_wrong_or_missing_id(self):
        client, _ = self.client(metadata())
        self.assertEqual(client.match('tt1', 'movie')['status'], 'matched')
        for guids in ([{'id': 'imdb://tt2'}], []):
            value = metadata()
            value['MediaContainer']['Metadata'][0]['Guid'] = guids
            client, _ = self.client(value)
            self.assertEqual(client.match('tt1', 'movie')['status'], 'id_mismatch')

    def test_no_match_ambiguity_and_wrong_type(self):
        for value, expected in [({'MediaContainer': {}}, 'not_found'),
                                ({'MediaContainer': {'Metadata': [{}, {}]}}, 'ambiguous')]:
            client, _ = self.client(value)
            self.assertEqual(client.match('tt1', 'movie')['status'], expected)
        value = metadata()
        value['MediaContainer']['Metadata'][0]['type'] = 'show'
        client, _ = self.client(value)
        self.assertEqual(client.match('tt1', 'movie')['status'], 'type_or_key_mismatch')

    def test_token_in_header_and_cloud_write_endpoint(self):
        client, opener = self.client({}, {})
        client.set_rating(KEY, 8)
        client.set_rating(KEY, None)
        for request in opener.requests:
            self.assertNotIn('synthetic-test-token', request.full_url)
            self.assertEqual(request.get_header('X-plex-token'), 'synthetic-test-token')
        self.assertEqual(opener.requests[1].method, 'PUT')
        self.assertTrue(opener.requests[1].full_url.startswith(METADATA + '/actions/rate?'))
        self.assertIn('rating=-1', opener.requests[2].full_url)

    def test_redirects_refused_and_errors_sanitized(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid'))
        failure = HTTPError('https://example.invalid?secret=synthetic-test-token', 401, 'private body', {}, None)
        client, _ = self.client(failure)
        with self.assertRaises(ImportProblem) as caught:
            client.get_rating(KEY)
        self.assertNotIn('synthetic-test-token', str(caught.exception))
        with self.assertRaises(ImportProblem):
            client._request('GET', 'https://example.invalid')

    def test_rate_limit_retry(self):
        failure = HTTPError(METADATA, 429, 'limited', {'Retry-After': '0'}, None)
        client, opener = self.client(failure, metadata(userRating=7))
        self.assertEqual(client.get_rating(KEY), 7)
        self.assertEqual(len(opener.requests), 3)

    def test_malformed_response_is_safe_error(self):
        for value in ([], {'MediaContainer': []}, {'MediaContainer': {'Metadata': [None]}}):
            client, _ = self.client(value)
            with self.assertRaises(ImportProblem):
                client.get_rating(KEY)


if __name__ == '__main__':
    unittest.main()
