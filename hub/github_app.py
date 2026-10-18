"""Token de instalação da App, renovado no cofre; nunca substitui o gh das contas."""
import base64
import datetime as dt
import json
import time
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from hub import cofre, conta


def _b64(valor):
    return base64.urlsafe_b64encode(valor).rstrip(b'=').decode()


def token(agora=None, abrir=None):
    """None quando não configurada. Configuração parcial falha sem cair no PAT.

    github.app.config = JSON {app_id, installation_id}; github.app.private_key
    = PEM. Ambos no cofre, assim como github.app.installation (cache do token).
    """
    config = cofre.ler('github.app.config')
    if not config:
        return None
    config = json.loads(config)
    app_id, installation_id = str(config['app_id']), str(config['installation_id'])
    if not app_id.isdigit() or not installation_id.isdigit():
        raise ValueError('IDs da GitHub App inválidos')
    agora = time.time() if agora is None else agora
    abrir = abrir or urllib.request.urlopen
    pasta = conta.pasta() / 'github-api'
    pasta.mkdir(parents=True, exist_ok=True, mode=0o700)
    with conta.trava(caminho=str(pasta / 'app.lock')):
        cache = json.loads(cofre.ler('github.app.installation') or '{}')
        if cache.get('installation_id') == installation_id and cache.get('expires', 0) > agora+120:
            return cache['token']
        pem = cofre.ler('github.app.private_key')
        if not pem:
            raise RuntimeError('GitHub App configurada sem chave no cofre')
        header = _b64(b'{"alg":"RS256","typ":"JWT"}')
        claims = _b64(json.dumps({'iat': int(agora)-60, 'exp': int(agora)+540, 'iss': app_id}).encode())
        unsigned = f'{header}.{claims}'.encode()
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        jwt = unsigned.decode() + '.' + _b64(key.sign(unsigned, padding.PKCS1v15(), hashes.SHA256()))
        req = urllib.request.Request(f'https://api.github.com/app/installations/{installation_id}/access_tokens',
                                     data=b'{}', method='POST', headers={
                                         'Authorization': 'Bearer '+jwt,
                                         'Accept': 'application/vnd.github+json',
                                         'Content-Type': 'application/json'})
        try:
            with abrir(req, timeout=30) as r:
                result = json.load(r)
        except Exception:
            # Nem JWT, nem corpo da resposta ou segredo nas mensagens de erro.
            raise RuntimeError('Não foi possível renovar token da GitHub App') from None
        expires = dt.datetime.fromisoformat(result['expires_at'].replace('Z', '+00:00')).timestamp()
        if expires <= agora+120:
            raise RuntimeError('GitHub App retornou token perto de expirar')
        cofre.definir('github.app.installation', json.dumps({
            'installation_id': installation_id, 'expires': expires, 'token': result['token']}), por='github-app')
        return result['token']
