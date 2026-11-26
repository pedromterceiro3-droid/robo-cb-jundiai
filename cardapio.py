"""
Cardápio digital da Tagme (cocobambu.tagme.menu) — o que mudou de um dia para o outro.

Lê a mesma lista pública que o site do cardápio usa (api.shopfood.io), guarda uma "foto" do dia
e compara com a anterior: item que ENTROU, que SAIU e que MUDOU DE PREÇO. Cada mudança vai para o
privado do gestor como cartão (foto do produto, código, descrição, preço) com botões.
Só leitura: não altera nada na Tagme nem no GIA.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import httpx

log = logging.getLogger("cardapio")

API = "https://api.shopfood.io/v2/stores/products"
LINK_CARDAPIO = "https://cocobambu.tagme.menu/menu/coco-bambu-jundiai"
LINK_GIA = "https://gia.cbinfo.com.br/tagme-disponibilidade"


def buscar(store_id: int = 1217) -> dict[str, dict]:
    """Itens que aparecem hoje no cardápio. Chave = produto:tamanho (cada tamanho tem código e preço)."""
    r = httpx.get(API, params={"store_id": store_id, "order_type": 5, "catalog_type": "menu"}, timeout=60,
                  headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131 Safari/537.36",
                           "Accept": "application/json", "Origin": "https://cocobambu.tagme.menu",
                           "Referer": LINK_CARDAPIO})
    r.raise_for_status()
    itens: dict[str, dict] = {}

    def andar(cats, caminho):
        for c in cats or []:
            cam = caminho + [c.get("name") or ""]
            for p in c.get("products") or []:
                if p.get("visible") is False or p.get("active") is False:
                    continue
                foto = (p.get("photos") or {}).get("medium") or (p.get("photos") or {}).get("large")
                base = {"produto_id": p.get("id"), "produto": (p.get("name") or "").strip(),
                        "descricao": (p.get("description") or "").strip(), "categoria": " > ".join(x for n, x in enumerate(cam) if x and (n == 0 or x != cam[n - 1])),
                        "foto": foto, "slug": p.get("slug")}
                tamanhos = [x for x in (p.get("complements") or []) if x.get("sku") or x.get("price")]
                if tamanhos:
                    for t in tamanhos:
                        itens[f"{p.get('id')}:{t.get('id')}"] = {
                            **base, "tamanho": (t.get("name") or "").strip(), "codigo": str(t.get("sku") or ""),
                            "preco": t.get("price") or t.get("from_price") or 0}
                else:
                    itens[f"{p.get('id')}:0"] = {**base, "tamanho": "", "codigo": str(p.get("sku") or ""),
                                                 "preco": p.get("price") or p.get("default_price") or 0}
            andar(c.get("children"), cam)

    andar(r.json(), [])
    return itens


def comparar(antes: dict, agora: dict) -> dict[str, list]:
    entrou = [agora[k] for k in agora if k not in antes]
    saiu = [antes[k] for k in antes if k not in agora]
    preco = []
    for k in agora.keys() & antes.keys():
        a, b = antes[k].get("preco") or 0, agora[k].get("preco") or 0
        if round(float(a), 2) != round(float(b), 2):
            preco.append({**agora[k], "preco_antes": a})
    ordem = lambda x: (x.get("categoria", ""), x.get("produto", ""), x.get("tamanho", ""))
    return {"entrou": sorted(entrou, key=ordem), "saiu": sorted(saiu, key=ordem), "preco": sorted(preco, key=ordem)}


def reais(v) -> str:
    try:
        return f"R$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".").replace(",00", "")
    except Exception:
        return "?"


def nome_item(i: dict) -> str:
    return i["produto"] + (f" – {i['tamanho']}" if i.get("tamanho") else "")


def legenda(i: dict, tipo: str, esc) -> str:
    """Texto do cartão (vai como legenda da foto: até 1024 caracteres)."""
    topo = {"entrou": "🟢 <b>ENTROU NO CARDÁPIO</b>", "saiu": "🔴 <b>SAIU DO CARDÁPIO</b>",
            "preco": "🟣 <b>PREÇO MUDOU</b>", "item": "🔎 <b>ITEM DO CARDÁPIO</b>"}[tipo]
    L = [topo, "", f"<b>{esc(nome_item(i))}</b>",
         f"Código: <code>{esc(i.get('codigo') or 'sem código')}</code>",
         f"Seção: {esc((i.get('categoria') or '').split(' > ')[-1])}"]
    if tipo == "preco":
        L.append(f"Preço: {reais(i.get('preco_antes'))} → <b>{reais(i.get('preco'))}</b>")
    else:
        L.append(f"Preço: <b>{reais(i.get('preco'))}</b>")
    if i.get("descricao"):
        d = i["descricao"]
        L += ["", esc(d[:500] + ("…" if len(d) > 500 else ""))]
    return "\n".join(L)


def agora_iso() -> str:
    return datetime.now().isoformat(timespec="minutes")


def resumo_json(itens: dict) -> str:
    return json.dumps({"itens": len(itens)}, ensure_ascii=False)
