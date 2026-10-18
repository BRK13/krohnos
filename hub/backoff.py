"""Repetição com espera nos limites de API. A cota do Linear (2.500/h) e a do GitHub estouraram às 01:44 UTC de 16/10.

`com_backoff(chamar, espera)`: `espera(exc)` devolve None se o erro não é de limite (sobe na hora), ou os segundos que o
servidor pediu (0 = o padrão, 1 s, 2 s, 4 s...). Espera acima de TETO (cota por hora esgotada) também sobe: dormir 40 min
dentro de uma tarefa não ajuda, e quem chama (voltar-quando, o tick) tenta na volta seguinte.
"""
import subprocess
import time
import urllib.error

TETO = 60.0


def com_backoff(chamar, espera, tentativas=3, dormir=None, teto=TETO):
    dormir = dormir or time.sleep
    for n in range(tentativas + 1):
        try:
            return chamar()
        except Exception as e:  # noqa: BLE001  só repete o que `espera` reconhece
            s = espera(e)
            if s is None or n == tentativas or s > teto:
                raise
            dormir(s or float(2 ** n))


def espera_linear(e, corpo=b"", mutacao=False):
    """429 e o RATELIMITED que o Linear devolve como HTTP 400 repetem; 5xx só em consulta (numa mutação o Linear pode ter
    gravado antes do 502 e repetir duplica comentário e sessão); o resto (query errada, 401) não."""
    if not isinstance(e, urllib.error.HTTPError):
        return None
    if e.code == 429 or (e.code in (500, 502, 503, 504) and not mutacao) or (e.code == 400 and b"RATELIMITED" in corpo):
        try:
            return float(e.headers.get("Retry-After") or 0)
        except (TypeError, ValueError):
            return 0.0
    return None


def espera_github(e):
    """429, 5xx e limite secundário repetem. Cota por hora esgotada ("API rate limit exceeded") não: só gastaria chamada."""
    if not isinstance(e, subprocess.CalledProcessError):
        return None
    msg = str(e.stderr or "") + str(e.stdout or "")
    if "secondary rate limit" in msg or any(f"HTTP {c}" in msg for c in (429, 500, 502, 503, 504)):
        return 0.0
    return None
