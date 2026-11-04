"""Wallet-auth protocol regression tests; no network or real wallets."""
import base64
import sqlite3
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from paperboard.wallet_auth import WalletAuth, ALPHABET, public_key


def address(key):
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    n, out = int.from_bytes(raw, 'big'), ''
    while n:
        n, r = divmod(n, 58)
        out = ALPHABET[r] + out
    return '1' * (len(raw) - len(raw.lstrip(b'\0'))) + out


class WalletTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"PAPER_WALLET_LOGIN": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE accounts(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, status TEXT DEFAULT "active", created_at REAL, updated_at REAL, last_login_at REAL)')
        self.auth = WalletAuth(SimpleNamespace(db=SimpleNamespace(conn=self.db), account_by_id=self.account))
        self.key = Ed25519PrivateKey.generate()
        self.wallet = address(self.key)

    def tearDown(self):
        self.db.close()

    def account(self, aid):
        row = self.db.execute('SELECT * FROM accounts WHERE id=?', (aid,)).fetchone()
        return dict(row) if row else None

    def sign(self, challenge, key=None):
        return base64.b64encode((key or self.key).sign(challenge['message'].encode())).decode()

    def challenge(self, **kw):
        return self.auth.challenge(self.wallet, 'https://api.seyruna.com', at=1000, **kw)

    def create(self):
        c = self.challenge(create_new=True)
        return self.auth.confirm(c['nonce'], self.sign(c), at=1001)

    def test_explicit_new_account_choice(self):
        with self.assertRaisesRegex(ValueError, 'link_existing_or_create_new'):
            self.challenge()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 0)

    def test_login_preserves_account(self):
        first = self.create()
        c = self.challenge()
        second = self.auth.confirm(c['nonce'], self.sign(c), at=1002)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 1)

    def test_link_preserves_email_id_and_points(self):
        self.db.execute('INSERT INTO accounts(email) VALUES("original@example.com")')
        self.db.execute('CREATE TABLE points(account_id INTEGER, amount INTEGER)')
        self.db.execute('INSERT INTO points VALUES(1,9000)')
        self.db.commit()
        acct = self.account(1)
        c = self.challenge(account=acct)
        result = self.auth.confirm(c['nonce'], self.sign(c), acct, at=1001)
        self.assertEqual((result['id'], result['email']), (1, 'original@example.com'))
        self.assertEqual(self.db.execute('SELECT amount FROM points').fetchone()[0], 9000)

    def test_link_needs_existing_session(self):
        self.db.execute('INSERT INTO accounts(email) VALUES("original@example.com")')
        self.db.commit()
        c = self.challenge(account=self.account(1))
        with self.assertRaisesRegex(ValueError, 'account_session_required'):
            self.auth.confirm(c['nonce'], self.sign(c), at=1001)

    def test_wrong_key(self):
        c = self.challenge(create_new=True)
        with self.assertRaisesRegex(ValueError, 'invalid_wallet_proof'):
            self.auth.confirm(c['nonce'], self.sign(c, Ed25519PrivateKey.generate()), at=1001)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 0)

    def test_replay(self):
        c = self.challenge(create_new=True)
        self.auth.confirm(c['nonce'], self.sign(c), at=1001)
        with self.assertRaisesRegex(ValueError, 'expired_wallet_proof'):
            self.auth.confirm(c['nonce'], self.sign(c), at=1002)

    def test_expiry(self):
        c = self.challenge(create_new=True)
        with self.assertRaisesRegex(ValueError, 'expired_wallet_proof'):
            self.auth.confirm(c['nonce'], self.sign(c), at=1300)

    def test_message_binding(self):
        c = self.challenge(create_new=True)
        for expected in ['https://api.seyruna.com', self.wallet, 'Action: create', 'Account: new', c['nonce']]:
            self.assertIn(expected, c['message'])
        changed = dict(c, message=c['message'].replace('Action: create', 'Action: login'))
        with self.assertRaisesRegex(ValueError, 'invalid_wallet_proof'):
            self.auth.confirm(c['nonce'], self.sign(changed), at=1001)

    def test_wallet_cannot_link_second_account(self):
        self.create()
        self.db.execute('INSERT INTO accounts(email) VALUES("other@example.com")')
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'wallet_already_linked'):
            self.challenge(account=self.account(2))

    def test_account_cannot_link_second_wallet(self):
        acct = self.create()
        other = Ed25519PrivateKey.generate()
        c = self.auth.challenge(address(other), 'https://api.seyruna.com', acct, at=1002)
        with self.assertRaisesRegex(ValueError, 'wallet_link_conflict'):
            self.auth.confirm(c['nonce'], self.sign(c, other), acct, at=1003)

    def test_shapes_and_rate_limit(self):
        for value in ['bad', '0' * 44, '1' * 31, '1' * 33]:
            with self.assertRaises(ValueError):
                public_key(value)
        for _ in range(5):
            self.challenge(create_new=True)
        with self.assertRaisesRegex(ValueError, 'wallet_rate_limited'):
            self.challenge(create_new=True)

    def test_disabled_is_fail_closed(self):
        with patch.dict("os.environ", {"PAPER_WALLET_LOGIN": "0"}):
            self.assertFalse(self.auth.enabled)
            with self.assertRaisesRegex(ValueError, "wallet_auth_unavailable"):
                self.challenge(create_new=True)

    def test_closed_account(self):
        acct = self.create()
        c = self.challenge()
        self.db.execute('UPDATE accounts SET status="closed" WHERE id=?', (acct['id'],))
        self.db.commit()
        with self.assertRaises(ValueError):
            self.auth.confirm(c['nonce'], self.sign(c), at=1002)


class RouterTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from paperboard.settings import Settings
        from paperboard.db import DB
        from paperboard.service import Service
        from paperboard.http_app import Router
        from paperboard.xapi import MockXClient
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict("os.environ", {"PAPER_WALLET_LOGIN": "1", "PAPER_MOCK": "1", "PAPER_EMAIL_PROVIDER": "mock", "PAPER_PUBLIC_BASE_URL": "https://api.seyruna.com"})
        env.start()
        self.addCleanup(env.stop)
        self.settings = Settings()
        self.db = DB(temp.name + '/test.sqlite', verbose=False)
        self.addCleanup(self.db.close)
        x = MockXClient(self.settings)
        self.service = Service(self.db, x, self.settings)
        self.service.ensure_competition()
        self.router = Router(self.service, self.settings, self.db, x)
        self.key = Ed25519PrivateKey.generate()
        self.wallet = address(self.key)

    def call(self, path, body=None, cookie=None, method='POST'):
        return self.router.dispatch(method, path, {}, {}, {'PBSD': cookie} if cookie else {}, body)

    def test_routes_create_session_and_keep_email_unverified(self):
        self.assertTrue(self.call('/api/config', method='GET')[1]['walletAuth']['enabled'])
        c = self.call('/api/account/wallet-auth/challenge', {'wallet': self.wallet, 'createNew': True})
        self.assertEqual(c[0], 200)
        signed = base64.b64encode(self.key.sign(c[1]['message'].encode())).decode()
        result = self.call('/api/account/wallet-auth/confirm', {'nonce': c[1]['nonce'], 'signature': signed})
        self.assertEqual(result[0], 200)
        cookie = result[2]['Set-Cookie'].split(';')[0].split('=', 1)[1]
        self.assertIn('HttpOnly', result[2]['Set-Cookie'])
        account = self.call('/api/account', cookie=cookie, method='GET')[1]['account']
        self.assertEqual(account['login_wallet'], self.wallet)
        self.assertIsNone(account['email'])
        self.assertFalse(account['email_verified'])
        self.assertFalse(account['x_verified'])
        self.assertEqual(self.call('/api/account/wallet-auth/confirm', {'nonce': c[1]['nonce'], 'signature': signed})[0], 422)

    def test_email_link_flow_still_works(self):
        account, _, _ = self.service.create_or_touch_account('existing@example.com')
        token, _ = self.service.issue_magic_link(account['id'])
        result = self.router.dispatch('GET', '/api/auth/magic', {'token': token}, {}, {}, None)
        self.assertEqual(result[0], 302)
        cookie = result[2]['Set-Cookie'].split(';')[0].split('=', 1)[1]
        c = self.call('/api/account/wallet-auth/challenge', {'wallet': self.wallet}, cookie)
        signature = base64.b64encode(self.key.sign(c[1]['message'].encode())).decode()
        result = self.call('/api/account/wallet-auth/confirm', {'nonce': c[1]['nonce'], 'signature': signature}, cookie)
        self.assertEqual(result[0], 200)
        self.assertEqual(result[1]['accountId'], account['id'])
        self.assertEqual(self.service.account_by_id(account['id'])['email'], 'existing@example.com')

    def test_plain_form_cannot_link_a_wallet(self):
        result = self.router.dispatch('POST', '/api/account/wallet-auth/challenge', {},
                                      {'content-type': 'application/x-www-form-urlencoded'}, {},
                                      {'wallet': self.wallet, 'createNew': True})
        self.assertEqual(result[0], 415)

    def test_reserved_identifier_never_sends_email(self):
        result = self.call('/api/account/start', {'email': 'wallet-attacker@wallet.paperboard.invalid'})
        self.assertEqual(result[0], 422)

    def test_disabled_capability(self):
        with patch.dict('os.environ', {'PAPER_WALLET_LOGIN': '0'}):
            self.assertFalse(self.call('/api/config', method='GET')[1]['walletAuth']['enabled'])
            result = self.call('/api/account/wallet-auth/challenge', {'wallet': self.wallet, 'createNew': True})
            self.assertEqual(result[0], 422)
            self.assertEqual(result[1]['error'], 'wallet_auth_unavailable')


if __name__ == '__main__':
    unittest.main()
