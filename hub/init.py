"""`krohnos init`: primeira configuração guiada (ou `--sim`, sem perguntas, para o teste de instalação).

Grava: krohnos.toml (sem segredo, validado), chave mestra 0600 e cofre vazio (cofre.db) na pasta do cofre, o usuário dono
do painel (<dados>/painel/usuarios.db, hub/usuarios.py) e o primeiro projeto em <dados>/projetos/<id>.json.
Rodar de novo é seguro: o que já existe (toml, chave, cofre, usuários, projeto) é mantido, nunca trocado.
A senha nunca vai por argumento de linha de comando: use --dono-senha-env NOME_DA_VAR ou deixe gerar (impressa uma vez).
"""
import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tomllib
from pathlib import Path

from hub import config, sites, usuarios

CHAVE_MESTRA = "chave-mestra"
COFRE_DB = "cofre.db"
RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RE_ORG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
RE_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}(/[A-Za-z0-9][A-Za-z0-9._-]{0,99})?$")
MAX_ID = 63
SENHA_MIN = usuarios.SENHA_MIN


class InitErro(ValueError):
    pass


# ---------- validação ----------

def _host(v):
    v = (v or "").strip().lower()
    if not sites.RE_HOST.match(v):
        raise InitErro(f"domínio inválido (só o host, sem https:// nem caminho): {v!r}")
    return v


def _email(v):
    v = (v or "").strip()
    if not RE_EMAIL.match(v):
        raise InitErro(f"e-mail inválido: {v!r}")
    return v


def _org(v):
    v = (v or "").strip()
    if v and not RE_ORG.match(v):
        raise InitErro(f"organização do GitHub inválida: {v!r}")
    return v


def _id(v):
    v = (v or "").strip()
    if not sites.RE_ID.match(v) or len(v) > MAX_ID:
        raise InitErro(f"id inválido (minúsculas, números e hífen, até {MAX_ID} caracteres): {v!r}")
    return v


def _repo(v, org):
    v = (v or "").strip()
    if not v:
        return ""
    if not RE_REPO.match(v):
        raise InitErro(f"repositório inválido: {v!r}")
    if "/" not in v:
        if not org:
            raise InitErro("repositório sem organização: use dono/repo ou informe --org")
        v = f"{org}/{v}"
    return v


def _senha(v):
    if "\n" in v or "\r" in v:
        raise InitErro("a senha não pode ter quebra de linha")
    if len(v) < SENHA_MIN:
        raise InitErro(f"a senha precisa ter pelo menos {SENHA_MIN} caracteres")
    return v


# ---------- perguntas ----------

def _perguntar(texto, padrao="", validar=None, entrada=input):
    while True:
        resp = entrada(f"{texto}" + (f" [{padrao}]" if padrao else "") + ": ").strip() or padrao
        try:
            return validar(resp) if validar else resp
        except InitErro as e:
            print(f"  {e}", file=sys.stderr)


def _caminho(chave):
    return Path(config.get(chave)).expanduser()


def _alvo_toml():
    """Onde gravar o krohnos.toml: KROHNOS_CONFIG; senão o que já existe; senão /etc/krohnos (se gravável) ou ~/.krohnos."""
    if os.environ.get("KROHNOS_CONFIG"):
        return Path(os.environ["KROHNOS_CONFIG"]).expanduser()
    if config.arquivo():
        return config.arquivo()
    etc = config.PADRAO_ARQUIVO[0]
    if os.access(etc.parent, os.W_OK):
        return etc
    return config.PADRAO_ARQUIVO[1].expanduser()


# ---------- gravação ----------

def escrever_atomico(caminho, conteudo, modo=0o644, so_se_novo=False):
    """tmp na mesma pasta (modo definido desde o primeiro byte) + fsync + os.replace: nunca deixa arquivo pela metade.
    so_se_novo=True usa link O_EXCL em vez de replace: devolve False (sem tocar) se o destino já existe."""
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(f".{caminho.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, modo)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(conteudo)
            f.flush()
            os.fsync(f.fileno())
        if so_se_novo:
            try:
                os.link(tmp, caminho)
            except FileExistsError:
                return False
            except OSError:  # sem hardlink (FS que não suporta): O_EXCL direto no destino, com o modo desde o 1º byte
                try:
                    fd2 = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_EXCL, modo)
                except FileExistsError:
                    return False
                with os.fdopen(fd2, "w", encoding="utf-8") as f2:
                    f2.write(conteudo)
                    f2.flush()
                    os.fsync(f2.fileno())
        else:
            os.replace(tmp, caminho)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _existe_nao_vazio(caminho):
    try:
        return caminho.stat().st_size > 0
    except FileNotFoundError:
        return False


def _pasta_privada(pasta):
    """Pasta 0700 (cria; se já existe e é nossa, aperta)."""
    pasta.mkdir(parents=True, exist_ok=True, mode=0o700)
    if pasta.stat().st_uid == os.getuid():
        os.chmod(pasta, 0o700)


def montar_toml(dados):
    return config._serializar(dados).replace("`python -m hub.config migrar`", "`krohnos init`")


def validar_toml(texto):
    """ConfigError se o texto não é toml, tem seção/chave desconhecida ou segredo."""
    try:
        d = tomllib.loads(texto)
    except tomllib.TOMLDecodeError as e:
        raise config.ConfigError(f"toml inválido: {e}")
    config._procurar_segredo(d)
    for secao, tabela in d.items():
        if secao not in config.SECOES:
            raise config.ConfigError(f"seção desconhecida: {secao}")
        for nome in tabela:
            if nome not in config.SECOES[secao]:
                raise config.ConfigError(f"chave desconhecida: {secao}.{nome}")


def conferir_cofre(cofre):
    """InitErro se há cofre.db sem chave mestra: o cofre ficaria ilegível e criar outra chave seria silenciosamente errado."""
    if (cofre / COFRE_DB).exists() and not _existe_nao_vazio(cofre / CHAVE_MESTRA):
        raise InitErro(f"{cofre / COFRE_DB} existe mas {cofre / CHAVE_MESTRA} não: recupere a chave mestra do backup "
                       "(não vou criar outra por cima de um cofre existente)")


def garantir_chave(cofre):
    """Chave mestra 0600; se já existe (e não está vazia), não toca. Devolve True se criou."""
    chave = cofre / CHAVE_MESTRA
    if _existe_nao_vazio(chave):
        return False
    conferir_cofre(cofre)
    return escrever_atomico(chave, secrets.token_urlsafe(48) + "\n", 0o600, so_se_novo=not chave.exists()) or False


def garantir_cofre(cofre):
    """Cofre vazio (SQLite) criado 0600 antes do sqlite. Existente é mantido. Frente 3 cifra com a chave mestra."""
    db = cofre / COFRE_DB
    try:
        os.close(os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        novo = True
    except FileExistsError:
        novo = False
        if db.stat().st_uid == os.getuid():
            os.chmod(db, 0o600)  # cofre existente com modo frouxo: aperta
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS segredos (nome TEXT PRIMARY KEY, valor BLOB NOT NULL, "
                    "atualizado TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        con.commit()
    finally:
        con.close()
    return novo


def garantir_dono(dados_dir, email, senha, antiga=False):
    """Cria o usuário dono do painel se ainda não há nenhum (login = e-mail; sem e-mail, "dono").
    antiga=True: é a PAINEL_SENHA já em uso, aceita sem a checagem de tamanho (como na migração do painel).
    Devolve a senha se criou agora, senão None (já havia usuário: nada muda)."""
    if usuarios.contar(pasta=dados_dir) > 0:
        return None
    try:
        (usuarios._inserir if antiga else usuarios.criar)(email or "dono", senha, "dono", pasta=dados_dir)
    except ValueError as ex:
        raise InitErro(str(ex)) from None
    return senha


def validar_projeto(projeto):
    """InitErro com os erros de hub.sites, sem gravar nada."""
    erros = sites.validar(projeto, f"{projeto.get('id')}.json")
    if erros:
        raise InitErro("projeto inválido:\n- " + "\n- ".join(erros))


def gravar_projeto(dados_dir, projeto):
    """Valida com hub.sites e grava <dados>/projetos/<id>.json; existente é mantido. Devolve o caminho ou None."""
    validar_projeto(projeto)
    _id(projeto["id"])  # nome de arquivo: nunca ../ nem fora do padrão
    destino = dados_dir / "projetos" / f"{projeto['id']}.json"
    if destino.exists():
        return None
    return destino if escrever_atomico(destino, json.dumps(projeto, ensure_ascii=False, indent=2) + "\n", so_se_novo=True) else None


def _exemplos_a_copiar(dados_dir):
    pares = []
    for sub in ("projetos", "tarefas"):
        origem = config.RAIZ / "exemplos" / sub
        for arq in sorted(origem.rglob("*")) if origem.is_dir() else []:
            destino = dados_dir / sub / arq.relative_to(origem)
            if arq.is_file() and not destino.exists():
                pares.append((arq, destino))
    return pares


def validar_exemplos(dados_dir):
    """Confere os JSON de projetos a copiar (InitErro se algum não valida), sem gravar nada."""
    for arq, _ in _exemplos_a_copiar(dados_dir):
        if arq.parent.name == "projetos" and arq.suffix == ".json":
            s, erros = sites._ler(arq)
            if erros:
                raise InitErro("exemplo inválido:\n- " + "\n- ".join(erros))


def copiar_exemplos(dados_dir):
    """Copia exemplos/projetos e exemplos/tarefas sem sobrescrever nada. Devolve os arquivos copiados."""
    copiados = []
    for arq, destino in _exemplos_a_copiar(dados_dir):
        destino.parent.mkdir(parents=True, exist_ok=True)
        if escrever_atomico(destino, arq.read_text(encoding="utf-8"), so_se_novo=True):
            copiados.append(destino)
    return copiados


def montar_projeto(pid, dominio, repo, nome, base="main"):
    return {
        "id": pid, "ids_antigos": [], "ativo": True, "nome": nome, "dominio": dominio,
        "hosts_alternativos": [], "hosts_citacao": [],
        "repo": {"github": repo, "base": base, "pacotes": "npm", "deploy": "dokploy"} if repo else None,
        "ids": {}, "marca": {"nome": nome, "aliases": [], "identificador": None, "nome_generico": False},
        "conselho": None, "contexto": f"{nome} ({dominio}). Recomende páginas em {dominio}.",
        "frase_canonica": None, "meta": None, "concorrentes": [], "monitor": None,
        "padrao": {"fora_do_google": False, "noindex_permitido": [], "treino_ia": None, "tipo_negocio": None,
                   "paginas_amostra": ["/"], "indexnow": None},
        "ranking": None,
    }


# ---------- fluxo ----------

def coletar(a, entrada=input, senha_digitada=getpass.getpass):
    """Resolve as respostas: argumentos primeiro; o que falta é perguntado (ou erro em --sim)."""
    r = {}

    def campo(nome, texto, padrao="", validar=None, obrigatorio=True):
        v = getattr(a, nome)
        if v is None and not a.sim:
            return _perguntar(texto, padrao, validar, entrada)
        if v is None and obrigatorio:
            raise InitErro(f"--{nome.replace('_', '-')} é obrigatório com --sim")
        return validar(v if v is not None else padrao) if validar else (v if v is not None else padrao)

    r["dominio"] = campo("dominio", "Domínio do painel (ex.: krohnos.minhaempresa-com.example)", validar=_host)
    r["dono_nome"] = campo("dono_nome", "Nome do dono", padrao=config.get("dono.nome") or "Dono", obrigatorio=False)
    r["dono_email"] = campo("dono_email", "E-mail do dono", validar=_email)
    r["org"] = campo("org", "Organização do GitHub (vazio = nenhuma)", validar=_org, obrigatorio=False)
    if a.senha_env:
        senha = os.environ.get(a.senha_env)
        if not senha:
            raise InitErro(f"a variável {a.senha_env} (--dono-senha-env) está vazia ou não existe")
        senha = _senha(senha)
    elif a.sim:
        senha = ""
    else:
        while True:
            senha = senha_digitada("Senha do dono (vazio = gerar uma aleatória): ")
            try:
                senha = _senha(senha) if senha else ""
                break
            except InitErro as e:
                print(f"  {e}", file=sys.stderr)
    r["senha"] = senha
    r["exemplo"] = a.exemplo
    dado_projeto = bool(a.projeto_id or a.projeto_dominio)
    if a.sim and not a.exemplo and not a.projeto_dominio:
        raise InitErro("--projeto-dominio é obrigatório com --sim (ou use --exemplo)")
    if not a.exemplo and not dado_projeto and not a.sim:
        resp = _perguntar("Usar os projetos de exemplo em vez de cadastrar o seu? (s/n)", "n", entrada=entrada)
        r["exemplo"] = resp.lower() in ("s", "sim")
    if not r["exemplo"] and (dado_projeto or not a.sim):
        dom = _host(a.projeto_dominio) if a.projeto_dominio else _perguntar("Domínio do projeto", validar=_host, entrada=entrada)
        padrao_id = dom.split(".")[0]
        pid = _id(a.projeto_id) if a.projeto_id else _id(padrao_id) if a.sim else _perguntar("Id do projeto (minúsculas e hífen)", padrao_id, _id, entrada)
        padrao_nome = pid.replace("-", " ").title()
        nome = a.projeto_nome or (padrao_nome if a.sim else _perguntar("Nome do projeto", padrao_nome, entrada=entrada))
        if a.projeto_repo is not None or a.sim:
            repo = _repo(a.projeto_repo, r["org"])
        else:
            repo = _perguntar("Repositório no GitHub (dono/repo, vazio = nenhum)", "", lambda v: _repo(v, r["org"]), entrada)
        base = a.projeto_base or ("main" if a.sim else _perguntar("Branch base", "main", entrada=entrada))
        r["projeto"] = montar_projeto(pid, dom, repo, nome, base)
    return r


def executar(r, destino_toml=None):
    """Faz tudo, de forma idempotente. Devolve um dict com o que foi criado (para imprimir e testar)."""
    toml = Path(destino_toml) if destino_toml else _alvo_toml()
    existente = tomllib.loads(toml.read_text(encoding="utf-8")) if toml.is_file() else {}
    caminhos = {k: str(_caminho(f"caminhos.{k}")) for k in ("dados", "cofre", "segredos")}
    caminhos.update(existente.get("caminhos", {}))
    dados = {"caminhos": caminhos,
             "dono": {"nome": r["dono_nome"], "email": r["dono_email"]},
             "github": {"org": r["org"]},
             "painel": {"dominio": r["dominio"]}}
    # sem [issues] provedor: vazio = fila própria até haver chave do Linear (fila_issues.provedor), e salvar a chave
    # depois (Conexões, linear.env) liga o Linear sem editar o toml
    saida = {"toml": toml, "criou_toml": False}
    cofre, segredos, dados_dir = (Path(caminhos[k]).expanduser() for k in ("cofre", "segredos", "dados"))
    # 1) tudo que pode falhar por entrada ruim, antes de qualquer escrita
    texto = None
    if toml.is_file():
        validar_toml(toml.read_text(encoding="utf-8"))  # mantido como está; só confere
    else:
        texto = montar_toml(dados)
        validar_toml(texto)
    if r.get("projeto"):
        validar_projeto(r["projeto"])
        _id(r["projeto"]["id"])
    if r.get("exemplo"):
        validar_exemplos(dados_dir)
    conferir_cofre(cofre)
    # 2) escrita
    if texto is not None:
        saida["criou_toml"] = escrever_atomico(toml, texto, so_se_novo=True)
    _pasta_privada(cofre)
    _pasta_privada(segredos)
    saida["chave_nova"] = garantir_chave(cofre)
    saida["cofre_novo"] = garantir_cofre(cofre)
    # instalação antiga (PAINEL_SENHA em painel.env) sem usuário ainda: o dono nasce com a senha de hoje, como na
    # migração da subida do painel; senão o init a trocaria por uma nova e a migração seria pulada (já haveria usuário)
    antiga = "" if r["senha"] else usuarios.senha_antiga(segredos)
    senha = r["senha"] or antiga or secrets.token_urlsafe(18)
    saida["senha"] = garantir_dono(dados_dir, r.get("dono_email"), senha, antiga=bool(antiga) and senha == antiga)
    saida["login"] = (r.get("dono_email") or "dono").strip().lower()
    saida["senha_antiga"] = bool(saida["senha"]) and bool(antiga)
    saida["senha_gerada"] = bool(saida["senha"]) and not r["senha"] and not antiga
    if saida["senha_gerada"]:  # impressa já: se algo adiante falhar, ela não se perde
        print(f"senha do dono (gerada, aparece só agora; guarde): {saida['senha']}", flush=True)
    saida["projeto"] = gravar_projeto(dados_dir, r["projeto"]) if r.get("projeto") else None
    saida["exemplos"] = copiar_exemplos(dados_dir) if r.get("exemplo") else []
    saida["pastas"] = {"cofre": cofre, "segredos": segredos, "dados": dados_dir}
    return saida


def _relatar(s):
    p = print
    p(f"krohnos.toml: {s['toml']} ({'criado' if s['criou_toml'] else 'já existia, mantido'})")
    p(f"chave mestra: {s['pastas']['cofre'] / CHAVE_MESTRA} ({'criada' if s['chave_nova'] else 'já existia, mantida'})")
    p(f"cofre: {s['pastas']['cofre'] / COFRE_DB} ({'criado' if s['cofre_novo'] else 'já existia, mantido'})")
    if s["senha"] is None:
        p("dono do painel: já existe usuário, mantido (esqueceu a senha: python3 -m hub.usuarios senha <email>)")
    elif s["senha_gerada"]:
        p(f"dono criado no painel: entre com {s['login']} e a senha impressa acima")
    elif s.get("senha_antiga"):
        p(f"dono criado no painel: entre com {s['login']} e a senha antiga do painel (PAINEL_SENHA), mantida")
    else:
        p(f"dono criado no painel: entre com {s['login']}")
    if s["projeto"]:
        p(f"projeto: {s['projeto']}")
    for arq in s["exemplos"]:
        p(f"exemplo copiado: {arq}")
    p("Pronto. Suba o painel com `docker compose up -d` e entre em https://<seu domínio>/")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="krohnos init", description=__doc__.split("\n\n")[0])
    ap.add_argument("--sim", action="store_true", help="sem perguntas: tudo por argumento")
    ap.add_argument("--dominio", help="domínio do painel")
    ap.add_argument("--dono-nome")
    ap.add_argument("--dono-email")
    ap.add_argument("--dono-senha-env", dest="senha_env", metavar="NOME_DA_VAR",
                    help="nome da variável de ambiente com a senha (nunca o valor na linha de comando)")
    ap.add_argument("--org", help="organização do GitHub")
    ap.add_argument("--exemplo", action="store_true", help="usar os projetos de exemplos/")
    ap.add_argument("--projeto-id")
    ap.add_argument("--projeto-nome")
    ap.add_argument("--projeto-dominio")
    ap.add_argument("--projeto-repo", help="dono/repo (ou só repo, com --org)")
    ap.add_argument("--projeto-base", help="branch base (padrão main)")
    a = ap.parse_args(argv)
    try:
        s = executar(coletar(a))
    except (InitErro, config.ConfigError) as e:
        print(f"krohnos init: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nkrohnos init: cancelado", file=sys.stderr)
        return 130
    _relatar(s)
    from hub import comecar  # import tardio: comecar importa este módulo
    comecar.anunciar()  # sem dono (nada o criou): o token do primeiro acesso aparece aqui; com dono, não faz nada
    return 0


if __name__ == "__main__":
    sys.exit(main())
