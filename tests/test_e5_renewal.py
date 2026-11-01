import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from utils.e5_renewal import (
    E5RenewalError,
    E5RenewalStore,
    build_authorize_url,
    build_webhook_callback_uri,
    parse_authorization_callback,
)


class OAuthUrlTests(unittest.TestCase):
    def test_authorize_url_contains_redirect_scope_and_state(self):
        url = build_authorize_url(
            "app-id", "http://localhost/e5sub", "random-state"
        )
        query = parse_qs(urlsplit(url).query)

        self.assertEqual(query["client_id"], ["app-id"])
        self.assertEqual(query["redirect_uri"], ["http://localhost/e5sub"])
        self.assertEqual(query["state"], ["random-state"])
        self.assertIn("Mail.Read", query["scope"][0])
        self.assertIn("offline_access", query["scope"][0])

    def test_callback_extracts_code_and_state(self):
        query = urlencode({"code": "auth-code", "state": "expected-state"})
        callback = parse_authorization_callback(
            f"http://localhost/e5sub/?{query}", "http://localhost/e5sub"
        )

        self.assertEqual(callback.code, "auth-code")
        self.assertEqual(callback.state, "expected-state")
        self.assertEqual(callback.error, "")

    def test_callback_rejects_different_redirect_uri(self):
        with self.assertRaises(E5RenewalError):
            parse_authorization_callback(
                "http://example.test/e5sub?code=x&state=y",
                "http://localhost/e5sub",
            )

    def test_callback_requires_state(self):
        with self.assertRaises(E5RenewalError):
            parse_authorization_callback(
                "http://localhost/e5sub?code=x", "http://localhost/e5sub"
            )

    def test_build_webhook_callback_uri_uses_public_https_host(self):
        self.assertEqual(
            build_webhook_callback_uri("https://bot.example.test"),
            "https://bot.example.test/e5/callback",
        )

    def test_webhook_callback_uri_requires_https(self):
        with self.assertRaises(E5RenewalError):
            build_webhook_callback_uri("http://bot.example.test")


class E5RenewalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = E5RenewalStore(Path(self.temp_dir.name) / "accounts.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_account(self, user_id=101, client_id="client-1"):
        return self.store.add_account(
            tg_user_id=user_id,
            alias="Personal",
            client_id=client_id,
            client_secret="app-secret",
            refresh_token="refresh-secret",
            profile={
                "id": "microsoft-id",
                "userPrincipalName": "user@example.test",
                "displayName": "Example User",
            },
        )

    def test_accounts_are_scoped_to_their_telegram_owner(self):
        account = self.add_account()

        self.assertEqual(self.store.count_for_user(101), 1)
        self.assertEqual(len(self.store.list_for_user(101)), 1)
        self.assertEqual(self.store.list_for_user(202), [])
        self.assertIsNone(self.store.get_owned_account(account.id, 202))
        self.assertFalse(self.store.delete_owned_account(account.id, 202))
        self.assertTrue(self.store.delete_owned_account(account.id, 101))
        self.assertEqual(self.store.count_for_user(101), 0)

    def test_pending_bind_can_be_replaced_and_cancelled(self):
        self.store.save_pending(101, "first", "secret-1", "state-1")
        self.store.save_pending(101, "second", "secret-2", "state-2")

        pending = self.store.get_pending(101)
        self.assertEqual(pending["client_id"], "second")
        self.assertEqual(pending["state"], "state-2")

        self.store.delete_pending(101)
        self.assertIsNone(self.store.get_pending(101))

    def test_pending_bind_can_be_looked_up_and_deleted_by_oauth_state(self):
        self.store.save_pending(101, "client", "secret", "unique-state")

        pending = self.store.get_pending_by_state("unique-state")
        self.assertEqual(pending["tg_user_id"], 101)

        self.store.delete_pending_by_state("unique-state")
        self.assertIsNone(self.store.get_pending(101))

    def test_success_resets_failures_and_rotates_refresh_token(self):
        account = self.add_account()
        self.store.record_failure(account.id, "temporary", 5)
        self.store.record_success(
            account.id,
            "rotated-refresh-token",
            {
                "id": "microsoft-id",
                "userPrincipalName": "user@example.test",
                "displayName": "Example User",
            },
        )

        updated = self.store.get_owned_account(account.id, 101)
        self.assertEqual(updated.error_count, 0)
        self.assertEqual(updated.last_error, "")
        self.assertEqual(updated.refresh_token, "rotated-refresh-token")
        self.assertTrue(updated.last_success_at)

    def test_repeated_failures_pause_but_do_not_delete_account(self):
        account = self.add_account()

        self.assertEqual(self.store.record_failure(account.id, "failure", 2), (1, False))
        self.assertEqual(self.store.record_failure(account.id, "failure", 2), (2, True))
        self.assertEqual(self.store.list_enabled(), [])
        self.assertFalse(self.store.get_owned_account(account.id, 101).enabled)
        self.assertEqual(self.store.count_for_user(101), 1)

    def test_duplicate_application_is_rejected_per_owner(self):
        self.add_account()
        with self.assertRaises(ValueError):
            self.add_account()
        self.add_account(user_id=202)
        self.assertEqual(self.store.count_for_user(202), 1)


if __name__ == "__main__":
    unittest.main()
