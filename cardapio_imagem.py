"""Imagem do "o que mudou no cardápio da Tagme" no estilo do quadro de vinhos:
faixa vinho com o título e três colunas (Saiu / Entrou / Preço mudou), cada item com nome,
código numa etiqueta e uma linha explicando. Desenhada com Pillow (sem navegador)."""
from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFont

VINHO = (91, 21, 48)
FUNDO = (250, 248, 247)
CARTAO = (255, 255, 255)
BORDA = (234, 228, 228)
TEXTO = (33, 30, 30)
CINZA = (110, 104, 104)
LINHA = (238, 232, 232)
ETQ_FUNDO = (249, 228, 234)
ETQ_TEXTO = (110, 22, 52)
CORES = {"saiu": (220, 53, 69), "entrou": (40, 160, 90), "preco": (201, 142, 170)}

_PASTAS = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fontes"),
           "/usr/share/fonts/truetype/liberation", "/usr/share/fonts/truetype/liberation2",
           "/usr/share/fonts/truetype/dejavu", "C:/Windows/Fonts"]
_FONTES = {
    "titulo": ["LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf", "georgiab.ttf", "timesbd.ttf"],
    "negrito": ["LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf"],
    "normal": ["LiberationSans-Regular.ttf", "DejaVuSans.ttf", "arial.ttf"],
}


def _fonte(tipo: str, tam: int):
    for pasta in _PASTAS:
        for nome in _FONTES[tipo]:
            p = os.path.join(pasta, nome)
            if os.path.exists(p):
                return ImageFont.truetype(p, tam)
    return ImageFont.load_default(size=tam)


def _quebrar(d: ImageDraw.ImageDraw, texto: str, fonte, largura: int) -> list[str]:
    linhas, atual = [], ""
    for palavra in (texto or "").split():
        teste = f"{atual} {palavra}".strip()
        if d.textlength(teste, font=fonte) <= largura:
            atual = teste
        else:
            if atual:
                linhas.append(atual)
            atual = palavra
    if atual:
        linhas.append(atual)
    return linhas or [""]


def gerar(colunas: list[dict], titulo: str, rodape: str = "", largura: int = 1600) -> bytes:
    """colunas: [{"tipo": "saiu"|"entrou"|"preco", "titulo": str, "vazio": str,
                  "itens": [{"nome", "codigo", "detalhe"}]}] → PNG em bytes."""
    S = largura / 1600  # escala
    f_tit = _fonte("titulo", int(46 * S))
    f_col = _fonte("titulo", int(34 * S))
    f_nome = _fonte("negrito", int(25 * S))
    f_cod = _fonte("negrito", int(22 * S))
    f_det = _fonte("normal", int(22 * S))
    f_rod = _fonte("normal", int(20 * S))
    margem, gap, faixa = int(40 * S), int(28 * S), int(96 * S)
    pad = int(26 * S)
    larg_col = (largura - 2 * margem - 2 * gap) // 3
    larg_txt = larg_col - 2 * pad
    rascunho = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    def medir(col) -> list[tuple]:
        """Monta os blocos (linhas já quebradas) e devolve com a altura de cada um."""
        blocos = []
        if not col["itens"]:
            linhas = _quebrar(rascunho, col.get("vazio", "Nada."), f_det, larg_txt)
            blocos.append(("vazio", linhas, len(linhas) * int(36 * S)))
            return blocos
        for it in col["itens"]:
            cod = str(it.get("codigo") or "")
            larg_cod = (rascunho.textlength(cod, font=f_cod) + 2 * int(10 * S)) if cod else 0
            nome_l = _quebrar(rascunho, it["nome"], f_nome, larg_txt)
            # a etiqueta do código vai ao lado da última linha do nome, ou embaixo se não couber
            cod_embaixo = cod and rascunho.textlength(nome_l[-1], font=f_nome) + int(12 * S) + larg_cod > larg_txt
            det_l = _quebrar(rascunho, it.get("detalhe", ""), f_det, larg_txt) if it.get("detalhe") else []
            h = len(nome_l) * int(36 * S) + (int(40 * S) if cod_embaixo else 0) + len(det_l) * int(32 * S) + int(30 * S)
            blocos.append(("item", (nome_l, cod, larg_cod, cod_embaixo, det_l), h))
        return blocos

    medidas = [medir(c) for c in colunas]
    topo_cartao = faixa + int(30 * S)
    cab = int(74 * S)
    alt_cartoes = max(cab + sum(b[2] for b in m) + pad for m in medidas)
    altura = topo_cartao + alt_cartoes + int(30 * S) + (int(44 * S) if rodape else 0)

    img = Image.new("RGB", (largura, altura), FUNDO)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, largura, faixa], fill=VINHO)
    d.text((margem, faixa // 2), titulo, font=f_tit, fill=(255, 255, 255), anchor="lm")

    for i, (col, blocos) in enumerate(zip(colunas, medidas)):
        x0 = margem + i * (larg_col + gap)
        d.rounded_rectangle([x0, topo_cartao, x0 + larg_col, topo_cartao + alt_cartoes], radius=int(18 * S),
                            fill=CARTAO, outline=BORDA, width=max(1, int(2 * S)))
        cy = topo_cartao + int(40 * S)
        r = int(10 * S)
        d.ellipse([x0 + pad, cy - r, x0 + pad + 2 * r, cy + r], fill=CORES[col["tipo"]])
        tit = f"{col['titulo']} ({len(col['itens'])})"
        cabe = larg_col - 2 * pad - 2 * r - int(14 * S)
        fc, tam = f_col, int(34 * S)
        while d.textlength(tit, font=fc) > cabe and tam > 18:  # título da coluna diminui se não couber
            tam -= 2
            fc = _fonte("titulo", tam)
        d.text((x0 + pad + 2 * r + int(14 * S), cy), tit, font=fc, fill=TEXTO, anchor="lm")
        y = topo_cartao + cab
        for n, (tipo, conteudo, h) in enumerate(blocos):
            if tipo == "vazio":
                for ln in conteudo:
                    d.text((x0 + pad, y), ln, font=f_det, fill=CINZA)
                    y += int(36 * S)
                continue
            nome_l, cod, larg_cod, cod_embaixo, det_l = conteudo
            for k, ln in enumerate(nome_l):
                d.text((x0 + pad, y), ln, font=f_nome, fill=TEXTO)
                if k == len(nome_l) - 1 and cod and not cod_embaixo:
                    cx = x0 + pad + d.textlength(ln, font=f_nome) + int(12 * S)
                    _etiqueta(d, cx, y - int(2 * S), cod, larg_cod, f_cod, S)
                y += int(36 * S)
            if cod and cod_embaixo:
                _etiqueta(d, x0 + pad, y, cod, larg_cod, f_cod, S)
                y += int(40 * S)
            for ln in det_l:
                d.text((x0 + pad, y + int(2 * S)), ln, font=f_det, fill=CINZA)
                y += int(32 * S)
            y += int(14 * S)
            if n < len(blocos) - 1:
                d.line([x0 + pad, y, x0 + larg_col - pad, y], fill=LINHA, width=max(1, int(2 * S)))
            y += int(16 * S)
    if rodape:
        d.text((margem, altura - int(34 * S)), rodape, font=f_rod, fill=CINZA, anchor="lm")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _etiqueta(d, x, y, texto, larg, fonte, S):
    alt = int(32 * S)
    d.rounded_rectangle([x, y, x + larg, y + alt], radius=int(8 * S), fill=ETQ_FUNDO)
    d.text((x + larg / 2, y + alt / 2), texto, font=fonte, fill=ETQ_TEXTO, anchor="mm")
