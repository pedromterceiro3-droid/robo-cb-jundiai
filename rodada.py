"""Uma "rodada" do robô no GitHub (roda de 5 em 5 minutos).

Cada rodada:
  1. abre os dados guardados (estado.enc, criptografado);
  2. responde ao que chegou no Telegram desde a última rodada (fotos, botões, comandos, "baixei");
  3. envia o que estava marcado entre a última rodada e agora (cobranças, listas, lembretes...);
  4. confere a fila da Tagme e as baixas em atraso;
  5. guarda os dados de novo, criptografados.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

PASTA = Path(__file__).parent
DADOS = PASTA / "dados"
ARQ_ESTADO = PASTA / "estado.enc"
ARQ_INICIAL = PASTA / "estado_inicial.enc"

# ---- 0) .env vindo do segredo ROBO_ENV do GitHub ----
if os.getenv("ROBO_ENV"):
    (PASTA / ".env").write_text(os.environ["ROBO_ENV"].replace("\r", "").strip() + "\n", encoding="utf-8")
from dotenv import load_dotenv  # noqa: E402

load_dotenv(PASTA / ".env")
SENHA = (os.getenv("CHAVE_ESTADO") or "").strip()
if not SENHA:
    sys.exit("Falta CHAVE_ESTADO no segredo ROBO_ENV (rode o EXPORTAR_PARA_GITHUB no PC).")

import cofre  # noqa: E402

# ---- 1) dados guardados: precisam estar na pasta ANTES de abrir o bot.py ----
origem = ARQ_ESTADO if ARQ_ESTADO.exists() else (ARQ_INICIAL if ARQ_INICIAL.exists() else None)
if origem:
    n = cofre.desempacotar(origem.read_bytes(), DADOS, SENHA)
    print(f"Estado aberto de {origem.name}: {n} arquivo(s)")
else:
    print("Nenhum estado guardado ainda: começando do zero")

import bot as B  # noqa: E402

log = B.log

# Botão tocado alguns minutos antes: o Telegram não aceita mais o "ok" do toque,
# mas o resto (atualizar a ocorrência, a lista etc.) precisa continuar.
from telegram import CallbackQuery  # noqa: E402

_answer_original = CallbackQuery.answer


async def _answer_tolerante(self, *a, **k):
    try:
        return await _answer_original(self, *a, **k)
    except Exception as e:
        log.info("Toque no botão respondido tarde (ok): %s", e)


CallbackQuery.answer = _answer_tolerante
ARQ_RODADA = B.DADOS / "rodada.json"
MAX_ATRASO_COBRANCA = timedelta(minutes=90)   # cobrança muito atrasada não sai mais
MAX_ATRASO_LEMBRETE = timedelta(minutes=60)


def ctx(app, data=None):
    return SimpleNamespace(bot=app.bot, application=app, job=SimpleNamespace(data=data),
                           job_queue=None, bot_data={}, chat_data={}, user_data={})


def _dias(desde: datetime, agora: datetime):
    d = desde.date() - timedelta(days=1)
    while d <= agora.date():
        yield d
        d += timedelta(days=1)


def eventos(desde: datetime, agora: datetime):
    """Tudo que estava marcado no intervalo (desde, agora]. Retorna (chave, previsto, tipo, dado)."""
    saida = []
    ref = B.CFG.get("reforco", {})
    # cobranças (mesma regra do agendar() do bot.py)
    for cob in B.CFG.get("cobrancas", []):
        intervalo = int(cob.get("reforco_intervalo_min", ref.get("intervalo_min", 30)))
        maximo = int(cob.get("reforco_maximo", ref.get("maximo", 3)))
        horarios = [(B.hora(cob["aviso"]), 0)]
        prazo = datetime.combine(date.today(), B.hora(cob["prazo"]))
        antes = int(cob.get("aviso_antes_prazo_min", B.CFG.get("aviso_antes_prazo_min", 0)))
        if antes:
            t2 = (prazo - timedelta(minutes=antes)).time()
            if (t2.hour, t2.minute) != B.hm(cob["aviso"]):
                horarios.append((t2, -1))
        for k in range(maximo + 1):
            horarios.append(((prazo + timedelta(minutes=intervalo * k)).time(), k + 1))
        for t, rod in horarios:
            depois_meia_noite = (t.hour, t.minute) < B.hm(cob["aviso"])
            for d in _dias(desde, agora):
                base = d - timedelta(days=1) if depois_meia_noite else d
                if not B.roda_hoje(cob, base):
                    continue
                prev = datetime.combine(d, t.replace(tzinfo=None), tzinfo=B.FUSO)
                if desde < prev <= agora:
                    saida.append((f"cob|{cob['nome']}|{rod}|{prev:%Y-%m-%dT%H:%M}", prev, "cobranca",
                                  {"cobranca": cob, "rodada": rod}))
    # envios diários (resumo, lista de ocorrências, reservas, relógio, semanal)
    for nome, func, h, dias_ptb, janela in B.envios_diarios():
        for d in _dias(agora - timedelta(hours=janela), agora):
            if (d.weekday() + 1) % 7 not in dias_ptb:
                continue
            prev = datetime.combine(d, h.replace(tzinfo=None), tzinfo=B.FUSO)
            if prev <= agora <= prev + timedelta(hours=janela):
                saida.append((f"env|{nome}|{d}", prev, "diario", (nome, func)))
    # lembretes personalizados
    for i, lem in enumerate(B.CFG.get("lembretes", [])):
        if not lem.get("ativo", True):
            continue
        dias = [B.DIAS[x] for x in lem.get("dias", B.DIAS.keys())]
        for d in _dias(desde, agora):
            if d.weekday() not in dias:
                continue
            prev = datetime.combine(d, B.hora(lem["horario"]).replace(tzinfo=None), tzinfo=B.FUSO)
            if desde < prev <= agora and agora - prev <= MAX_ATRASO_LEMBRETE:
                saida.append((f"lem|{i}|{prev:%Y-%m-%dT%H:%M}", prev, "lembrete", lem))
    return sorted(saida, key=lambda e: e[1])


async def processar_mensagens(app, estado):
    """Pega tudo que chegou no Telegram desde a última rodada e responde."""
    offset = estado.get("offset")
    total = 0
    for _ in range(20):
        try:
            ups = await app.bot.get_updates(offset=offset, timeout=0, limit=100,
                                            allowed_updates=["message", "edited_message", "callback_query"])
        except Exception as e:
            log.warning("Não consegui ler as mensagens do Telegram: %s", e)
            break
        if not ups:
            break
        for u in ups:
            offset = u.update_id + 1
            estado["offset"] = offset
            try:
                await app.process_update(u)
            except Exception:
                log.exception("Erro ao tratar mensagem")
            total += 1
    if offset is not None:  # confirma para o Telegram que já lemos
        try:
            await app.bot.get_updates(offset=offset, timeout=0, limit=1)
        except Exception:
            pass
    log.info("Mensagens tratadas nesta rodada: %s", total)


async def enviar_marcados(app, estado, agora):
    feitos = estado.setdefault("feitos", {})
    ultima = estado.get("ultima")
    desde = datetime.fromisoformat(ultima) if ultima else agora - timedelta(minutes=15)
    desde = max(desde, agora - timedelta(hours=40))
    evs = [e for e in eventos(desde, agora) if e[0] not in feitos]
    # da mesma cobrança, só manda a mensagem mais recente (se a rodada atrasou, não manda 3 seguidas)
    ultima_cob = {}
    for e in evs:
        if e[2] == "cobranca":
            ultima_cob[e[3]["cobranca"]["nome"]] = e[0]
    exec_hoje = B.ler(B.ARQ_EXEC, {})
    for chave, prev, tipo, dado in evs:
        feitos[chave] = agora.date().isoformat()
        try:
            if tipo == "cobranca":
                if chave != ultima_cob.get(dado["cobranca"]["nome"]) or agora - prev > MAX_ATRASO_COBRANCA:
                    log.info("Pulando cobrança atrasada/repetida: %s", chave)
                    continue
                log.info("Cobrança: %s", chave)
                await B.cobrar(ctx(app, dado))
            elif tipo == "diario":
                nome, func = dado
                if exec_hoje.get(nome) == prev.date().isoformat():
                    continue
                log.info("Envio diário: %s", chave)
                await B.registrar_envio(nome, func)(ctx(app))
            elif tipo == "lembrete":
                log.info("Lembrete: %s", chave)
                await B.enviar_lembrete(ctx(app, dado))
        except Exception:
            log.exception("Erro no envio %s", chave)
    # esquece marcas com mais de 3 dias
    limite = (agora.date() - timedelta(days=3)).isoformat()
    estado["feitos"] = {k: v for k, v in feitos.items() if v >= limite}


async def rodada():
    estado = B.ler(ARQ_RODADA, {})
    B.MSG_COBRANCA.update({int(k): v for k, v in estado.get("msg_cobranca", {}).items()})
    B._TG_ULTIMO_ERRO[0] = float(estado.get("tg_erro", 0))
    token = (os.getenv("TELEGRAM_TOKEN") or "").strip().strip("`'\" ")
    app = B.montar_app(token)
    await app.initialize()
    agora = datetime.now(B.FUSO)
    try:
        await processar_mensagens(app, estado)
        await enviar_marcados(app, estado, agora)
        # fila da Tagme
        if B.TG and B.tg_cfg().get("ativo", True):
            try:
                await B.vigiar_fila(ctx(app))
            except Exception:
                log.exception("Erro na fila da Tagme")
        # baixas em atraso (a cada ~10 min)
        ult_v = estado.get("vigiar")
        if not ult_v or agora - datetime.fromisoformat(ult_v) >= timedelta(minutes=9):
            try:
                await B.vigiar_atrasos(ctx(app))
            except Exception:
                log.exception("Erro ao vigiar atrasos")
            estado["vigiar"] = agora.isoformat(timespec="seconds")
        # espera tarefas que ficaram rodando (ex.: /testar)
        pend = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pend:
            await asyncio.wait(pend, timeout=240)
    finally:
        estado["ultima"] = agora.isoformat(timespec="seconds")
        estado["msg_cobranca"] = {str(k): v for k, v in list(B.MSG_COBRANCA.items())[-300:]}
        estado["tg_erro"] = B._TG_ULTIMO_ERRO[0]
        B.gravar(ARQ_RODADA, estado)
        try:
            await app.shutdown()
        except Exception:
            pass


def guardar():
    ARQ_ESTADO.write_bytes(cofre.empacotar(DADOS, SENHA))
    print(f"Estado guardado ({ARQ_ESTADO.stat().st_size // 1024} KB, criptografado)")


if __name__ == "__main__":
    try:
        asyncio.run(rodada())
    finally:
        guardar()
