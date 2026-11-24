"""Prepara a pasta 'para_github' com tudo que vai para o repositório.

- Cria (uma vez) a CHAVE_ESTADO no .env: é a senha que tranca os dados no GitHub.
- Tranca a pasta dados/ (ocorrências, IDs dos grupos, cadastros...) em estado_inicial.enc.
- NÃO copia o .env nem a pasta dados/ abertos: o repositório é público.
"""
import secrets
import shutil
from pathlib import Path

import cofre

PASTA = Path(__file__).parent
SAIDA = PASTA / "para_github"
ENV = PASTA / ".env"
ARQUIVOS = ["bot.py", "bar.py", "legado.py", "leankeep.py", "tagme.py", "config.toml", "requirements.txt",
            "rodada.py", "cofre.py", "exportar.py"]

if not ENV.exists():
    raise SystemExit("ERRO: não achei o arquivo .env nesta pasta.")

texto = ENV.read_text(encoding="utf-8-sig")
chave = None
for linha in texto.splitlines():
    if linha.strip().startswith("CHAVE_ESTADO="):
        chave = linha.split("=", 1)[1].strip()
if not chave:
    chave = secrets.token_urlsafe(32)
    ENV.write_text(texto.rstrip("\r\n") + f"\nCHAVE_ESTADO={chave}\n", encoding="utf-8")
    print("Criei a CHAVE_ESTADO no .env.")

if SAIDA.exists():
    shutil.rmtree(SAIDA)
(SAIDA / ".github" / "workflows").mkdir(parents=True)
for nome in ARQUIVOS:
    if not (PASTA / nome).exists():
        raise SystemExit(f"ERRO: falta o arquivo {nome} nesta pasta.")
    shutil.copy2(PASTA / nome, SAIDA / nome)
wf = PASTA / ".github" / "workflows" / "robo.yml"
if not wf.exists():
    wf = PASTA / "robo.yml"
shutil.copy2(wf, SAIDA / ".github" / "workflows" / "robo.yml")
shutil.copy2(wf, SAIDA / "robo.yml.txt")  # cópia para colar à mão, se precisar

dados = PASTA / "dados"
if dados.exists():
    (SAIDA / "estado_inicial.enc").write_bytes(cofre.empacotar(dados, chave))
    print(f"Dados atuais trancados em estado_inicial.enc ({len(list(dados.rglob('*')))} itens).")
else:
    print("Atenção: não achei a pasta dados/. O robô no GitHub vai começar do zero.")

print()
print("PRONTO! Pasta criada:", SAIDA)
print("Agora:")
print(" 1) Envie o CONTEÚDO da pasta para_github para o repositório no GitHub.")
print(" 2) Copie TODO o texto do .env (vai abrir no Bloco de Notas) para o segredo ROBO_ENV.")
