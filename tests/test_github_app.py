import io
import json
import tempfile
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from hub import github_app as a


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from pathlib import Path
        p = patch.object(a.conta, 'pasta', return_value=Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def test_unconfigured_preserves_gh(self):
        with patch.object(a.cofre, 'ler', return_value=None):
            self.assertIsNone(a.token())

    def test_cached_token(self):
        data = {'github.app.config': '{"app_id":1,"installation_id":2}',
                'github.app.installation': '{"installation_id":"2","expires":5000,"token":"test"}'}
        with patch.object(a.cofre, 'ler', side_effect=data.get):
            self.assertEqual(a.token(1000), 'test')

    def test_mint_and_store_only_in_vault(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        data = {'github.app.config': '{"app_id":1,"installation_id":2}', 'github.app.private_key': pem}
        captured = []
        def open_(req, **kw):
            captured.append(req)
            return io.StringIO('{"token":"installation-test","expires_at":"2026-10-18T00:00:00Z"}')
        with patch.object(a.cofre, 'ler', side_effect=data.get), patch.object(a.cofre, 'definir') as store:
            self.assertEqual(a.token(1000, open_), 'installation-test')
        self.assertEqual(captured[0].full_url, 'https://api.github.com/app/installations/2/access_tokens')
        self.assertEqual(store.call_args.args[0], 'github.app.installation')
        self.assertEqual(json.loads(store.call_args.args[1])['token'], 'installation-test')

    def test_partial_configuration_never_falls_back(self):
        with patch.object(a.cofre, 'ler', side_effect=lambda n: '{"app_id":1,"installation_id":2}' if n.endswith('config') else None):
            with self.assertRaisesRegex(RuntimeError, 'sem chave'):
                a.token(1000)
