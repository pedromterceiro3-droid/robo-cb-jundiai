"""Guarda a pasta dados/ criptografada (o repositório do GitHub é público).

Criptografia só com a biblioteca padrão do Python (funciona no Windows e no GitHub):
  - chave derivada da senha CHAVE_ESTADO com PBKDF2-SHA256
  - cifra: fluxo HMAC-SHA256 em modo contador (PRF-CTR) com nonce aleatório
  - integridade: HMAC-SHA256 sobre nonce + texto cifrado (encrypt-then-MAC)
"""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import zipfile
from pathlib import Path

MAGICO = b"ROBO1"
SAL = b"robo-leankeep-estado"


def _chaves(senha: str) -> tuple[bytes, bytes]:
    k = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), SAL, 200_000, dklen=64)
    return k[:32], k[32:]


def _fluxo(chave: bytes, nonce: bytes, tamanho: int) -> bytes:
    saida = bytearray()
    contador = 0
    while len(saida) < tamanho:
        saida += hmac.new(chave, nonce + contador.to_bytes(8, "big"), hashlib.sha256).digest()
        contador += 1
    return bytes(saida[:tamanho])


def cifrar(dados: bytes, senha: str) -> bytes:
    kc, km = _chaves(senha)
    nonce = os.urandom(16)
    fluxo = _fluxo(kc, nonce, len(dados))
    ct = bytes(a ^ b for a, b in zip(dados, fluxo))
    tag = hmac.new(km, MAGICO + nonce + ct, hashlib.sha256).digest()
    return MAGICO + nonce + tag + ct


def decifrar(bloco: bytes, senha: str) -> bytes:
    if not bloco.startswith(MAGICO):
        raise ValueError("arquivo de estado inválido")
    nonce, tag, ct = bloco[5:21], bloco[21:53], bloco[53:]
    kc, km = _chaves(senha)
    if not hmac.compare_digest(tag, hmac.new(km, MAGICO + nonce + ct, hashlib.sha256).digest()):
        raise ValueError("CHAVE_ESTADO errada ou arquivo de estado corrompido")
    fluxo = _fluxo(kc, nonce, len(ct))
    return bytes(a ^ b for a, b in zip(ct, fluxo))


def empacotar(pasta: Path, senha: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pasta.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(pasta).as_posix())
    return cifrar(buf.getvalue(), senha)


def desempacotar(bloco: bytes, pasta: Path, senha: str) -> int:
    pasta.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(decifrar(bloco, senha))) as z:
        nomes = [n for n in z.namelist() if not n.startswith("/") and ".." not in n]
        for n in nomes:
            destino = pasta / n
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(z.read(n))
    return len(nomes)
