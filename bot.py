"""Robô do Telegram que cobra os checklists de manutenção do Leankeep.

Modos:
  - leankeep: consulta o Leankeep e cobra só o que está pendente (precisa de
    LEANKEEP_USUARIO / LEANKEEP_SENHA / LEANKEEP_EMPRESA_ID no arquivo .env)
  - manual:   sem acesso ao Leankeep; manda o lembrete com botão "✅ Feito"
O modo é escolhido sozinho: se o .env tiver usuário do Leankeep, usa o modo leankeep.
"""
from __future__ import annotations

import asyncio
import contextvars
import html
import json
import logging
import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import tomllib
from dotenv import load_dotenv
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest
import httpx
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from leankeep import Atividade, Leankeep
from tagme import Tagme

PASTA = Path(__file__).parent
load_dotenv(PASTA / ".env")
CFG = tomllib.loads((PASTA / "config.toml").read_text(encoding="utf-8-sig"))
FUSO = ZoneInfo(CFG.get("fuso", "America/Sao_Paulo"))
DADOS = PASTA / "dados"
DADOS.mkdir(exist_ok=True)
ARQ_CADASTROS = DADOS / "cadastros.json"
ARQ_FEITOS = DADOS / "feitos.json"
ARQ_AJUSTES = DADOS / "ajustes.json"   # IDs gravados pelos comandos /configurar_topico e /sou_gestor
try:
    CFG.update({k: v for k, v in json.loads(ARQ_AJUSTES.read_text(encoding="utf-8")).items() if v})
except Exception:
    pass

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("robo")
logging.getLogger("httpx").setLevel(logging.WARNING)

DIAS = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "dom": 6}

LK = None
if os.getenv("LEANKEEP_USUARIO") and os.getenv("LEANKEEP_SENHA"):
    LK = Leankeep(os.environ["LEANKEEP_USUARIO"].strip(), os.environ["LEANKEEP_SENHA"],
                  os.getenv("LEANKEEP_EMPRESA_ID", "3802").strip(),
                  assumir_sessao=os.getenv("LEANKEEP_ASSUMIR_SESSAO", "").strip().lower() in ("sim", "s", "true", "1"),
                  arquivo_sessao=str(PASTA / "dados" / "sessao_leankeep.json"))
MODO = "leankeep" if LK else "manual"

TG = None
if os.getenv("TAGME_EMAIL") and os.getenv("TAGME_SENHA"):
    TG = Tagme(os.environ["TAGME_EMAIL"].strip(), os.environ["TAGME_SENHA"],
               str((CFG.get("tagme") or {}).get("venue_id", "1f1a2df6-d738-6510-844a-461e7b222271")),
               arquivo_sessao=str(PASTA / "dados" / "sessao_tagme.json"))


# ---------------- armazenamento simples ----------------
def ler(arq: Path, padrao):
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except Exception:
        return padrao


def gravar(arq: Path, dado):
    arq.write_text(json.dumps(dado, ensure_ascii=False, indent=2), encoding="utf-8")


def cadastros() -> dict:
    """{telegram_id: {"nome_leankeep": ..., "nome": ...}}"""
    return ler(ARQ_CADASTROS, {})


def normalizar(txt: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", txt or "")
    return "".join(c for c in t if not unicodedata.combining(c)).upper().strip()


def hoje() -> date:
    return datetime.now(FUSO).date()


def hora(txt: str) -> time:
    h, m = map(int, str(txt).split(":"))
    return time(h, m, tzinfo=FUSO)


def esc(t) -> str:
    return html.escape(str(t))


def _eh_emoji(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x2190 and ch not in "–—•·…"


def limpar_texto(texto: str) -> str:
    """No Telegram do usuário, linha que COMEÇA com emoji perde a quebra de linha.
    Mantém o emoji só na 1ª linha; nas demais troca ▫️ por • e tira o emoji inicial."""
    linhas = texto.split("\n")
    for i in range(1, len(linhas)):
        l = linhas[i]
        corpo = l.lstrip(" ")
        recuo = l[: len(l) - len(corpo)]
        # pula tags HTML no começo (<b>, <i>) para achar o 1º caractere visível
        import re
        m = re.match(r"((?:<[^>]+>)*)(.*)", corpo, re.S)
        tags, resto = m.group(1), m.group(2)
        if resto and _eh_emoji(resto[0]):
            sem = resto.lstrip("▫️▪️◽◾🔹🔸")
            bullet = sem != resto
            while sem and (_eh_emoji(sem[0]) or sem[0] in "\ufe0f\u200d"):
                sem = sem[1:]
            sem = sem.lstrip(" ")
            linhas[i] = recuo + ("• " if bullet else "") + tags + sem
    return "\n".join(linhas)


# ---------------- modo de teste ----------------
# Enquanto TESTE estiver ligado (só dentro do /testar), tudo que o robô mandaria
# para grupos ou colaboradores vai para o privado de quem pediu o teste.
TESTE = contextvars.ContextVar("teste", default=None)        # chat_id de quem está testando
EXEMPLO = contextvars.ContextVar("exemplo", default=None)    # atividades fictícias p/ o teste


def nome_destino(chat_id, thread=None) -> str:
    c, t = str(chat_id), int(thread or 0)
    sc, st = oc_saida()
    if sc and c == str(sc) and t == int(st or 0) and (sc, st) != oc_coleta():
        return "tópico Keepbot"
    if c == str(CFG.get("oc_grupo_chat_id")) and t == int(CFG.get("oc_topico_id") or 0):
        return "tópico onde a equipe posta as ocorrências"
    if c == str(CFG.get("grupo_chat_id")):
        return "tópico Leankeep diário"
    if c == str(CFG.get("oc_grupo_chat_id")):
        return "grupo de ocorrências"
    if c == str(CFG.get("fech_grupo_chat_id")):
        return "grupo de fechamento"
    if c == str(CFG.get("tagme_grupo_chat_id")):
        return "tópico Bot da Tagme"
    if c == str(CFG.get("gestor_chat_id")):
        return "seu privado"
    return "privado do colaborador"


def teclado_de_teste(teclado):
    """No teste, botões que mudam dados viram botões de demonstração.
    Os da Tagme (só consultam) continuam funcionando."""
    if not isinstance(teclado, InlineKeyboardMarkup):
        return teclado
    linhas = []
    for linha in teclado.inline_keyboard:
        nova = []
        for b in linha:
            dado = b.callback_data or ""
            if dado.startswith("tg|") or b.url:
                nova.append(b)
            else:
                nova.append(InlineKeyboardButton(b.text, callback_data="tx|demo"))
        linhas.append(nova)
    return InlineKeyboardMarkup(linhas)


async def enviar_grupo(bot, texto, **kw):
    """Envia no grupo da manutenção, dentro do tópico configurado (se houver)."""
    topico = CFG.get("topico_id") or None
    return await enviar(bot, CFG.get("grupo_chat_id"), texto, message_thread_id=topico, **kw)


async def enviar(bot, chat_id, texto, **kw):
    """Envia respeitando o limite de 4096 caracteres do Telegram."""
    quem_testa = TESTE.get()
    if quem_testa:
        destino = nome_destino(chat_id, kw.get("message_thread_id")) if chat_id else "destino ainda não configurado"
        chat_id = quem_testa
        kw.pop("message_thread_id", None)
        kw.pop("reply_to_message_id", None)
        if "reply_markup" in kw:
            kw["reply_markup"] = teclado_de_teste(kw["reply_markup"])
        texto = f"{texto}\n\n<i>TESTE – no dia a dia esta mensagem vai para: {esc(destino)}</i>"
    if not chat_id:
        return None
    ultima = None
    linhas_txt = limpar_texto(texto).split("\n")
    partes, atual = [], ""
    for linha in linhas_txt:
        if atual and len(atual) + len(linha) + 1 > 3900:
            partes.append(atual)
            atual = ""
        atual += linha + "\n"
    partes.append(atual)
    for i, p in enumerate(partes):
        extra = dict(kw) if i == len(partes) - 1 else {}
        if kw.get("message_thread_id"):
            extra["message_thread_id"] = kw["message_thread_id"]
        elif "message_thread_id" in extra:
            del extra["message_thread_id"]
        for tentativa in range(4):
            try:
                ultima = await bot.send_message(chat_id, p, parse_mode=ParseMode.HTML,
                                                disable_web_page_preview=True, **extra)
                break
            except (TimedOut, NetworkError) as e:  # internet instável: tenta de novo
                log.warning("Rede instável ao enviar para %s (tentativa %s): %s",
                            chat_id, tentativa + 1, e)
                await asyncio.sleep(10 * (tentativa + 1))
            except Exception as e:  # pessoa que nunca deu /start no privado, etc.
                log.warning("Falha ao enviar para %s: %s", chat_id, e)
                break
    return ultima


# ---------------- consulta ao Leankeep ----------------
def hm(txt: str) -> tuple[int, int]:
    h, m = map(int, str(txt).split(":"))
    return h, m


def vira_meia_noite(cob) -> bool:
    """Prazo depois da meia-noite (ex.: aviso 23:00, prazo 02:00)."""
    return hm(cob["prazo"]) < hm(cob["aviso"])


def data_ref(cob) -> date:
    """Dia a que a cobrança se refere. Na madrugada, o fechamento é do dia anterior."""
    agora = datetime.now(FUSO)
    if vira_meia_noite(cob) and (agora.hour, agora.minute) < hm(cob["aviso"]):
        return agora.date() - timedelta(days=1)
    return agora.date()


def txt_prazo(cob) -> str:
    return f"{cob['prazo']} (madrugada)" if vira_meia_noite(cob) else cob["prazo"]


async def buscar_pendentes(palavras: list[str] | None = None, dia: date | None = None,
                           excluir: list[str] | None = None):
    if EXEMPLO.get() is not None:
        return list(EXEMPLO.get())
    d = dia or hoje()
    dias_atras = int(CFG.get("incluir_atrasadas_dias", 0))
    try:
        ativs = await asyncio.wait_for(
            asyncio.to_thread(LK.pendentes, d - timedelta(days=dias_atras), d, CFG.get("unidade")),
            timeout=150)
    except asyncio.TimeoutError:
        raise RuntimeError("o Leankeep demorou mais de 2 minutos para responder.")
    if palavras:
        chaves = [normalizar(p) for p in palavras]
        ativs = [a for a in ativs if any(c in normalizar(a.descricao + " " + a.plano)
                                         for c in chaves)]
    if excluir:
        fora = [normalizar(p) for p in excluir]
        ativs = [a for a in ativs if not any(c in normalizar(a.descricao + " " + a.plano)
                                             for c in fora)]
    return ativs


def por_executor(ativs):
    grupos = defaultdict(list)
    for a in ativs:
        for nome in (a.executores or ["(sem executor definido)"]):
            grupos[nome].append(a)
    return grupos


def mencao(nome_leankeep: str, cads: dict) -> str:
    for tid, c in cads.items():
        if normalizar(c["nome_leankeep"]) == normalizar(nome_leankeep):
            return f'<a href="tg://user?id={tid}">{esc(c["nome"])}</a>'
    return f"<b>{esc(nome_leankeep)}</b>"


# ---------------- formatação das listas ----------------
SIGLAS = {"QGBT", "LED", "GLP", "CO2", "AC", "UPS", "QDF", "QD", "EPI", "JUN", "OSA", "CB", "TV", "SPDA"}


def legivel(txt: str) -> str:
    """'VERIFICAR O FUNCIONAMENTO DO QGBT' -> 'Verificar o funcionamento do QGBT'."""
    import re
    txt = re.sub(r"\s+", " ", (txt or "").strip())
    for suf in CFG.get("remover_sufixos", [" - JUN"]):
        if txt.upper().endswith(suf.upper()):
            txt = txt[: -len(suf)].strip()
    if txt != txt.upper():  # já está em letras normais
        return txt
    palavras = []
    for i, p in enumerate(txt.split(" ")):
        limpo = re.sub(r"[^\w]", "", p)
        if limpo in SIGLAS or any(ch.isdigit() for ch in limpo):
            palavras.append(p)
        else:
            palavras.append(p.capitalize() if i == 0 else p.lower())
    return " ".join(palavras)


def resumir_executores(nomes) -> str:
    """cbjunger01.cb … cbjunger06.cb -> cbjunger01–06.cb"""
    import re
    grupos = defaultdict(list)
    soltos = []
    for n in sorted(set(nomes)):
        m = re.fullmatch(r"(.*?)(\d+)(\D*)", n)
        if m:
            grupos[(m.group(1), m.group(3), len(m.group(2)))].append(int(m.group(2)))
        else:
            soltos.append(n)
    partes = []
    for (pre, suf, larg), nums in sorted(grupos.items()):
        nums.sort()
        if len(nums) > 2 and nums[-1] - nums[0] == len(nums) - 1:
            partes.append(f"{pre}{nums[0]:0{larg}d}–{nums[-1]:0{larg}d}{suf}")
        else:
            partes += [f"{pre}{n:0{larg}d}{suf}" for n in nums]
    return ", ".join(partes + soltos)


def sem_duplicadas(ativs):
    vistos, saida = set(), []
    for a in ativs:
        chave = a.numero or normalizar(a.descricao)
        if chave in vistos:
            continue
        vistos.add(chave)
        saida.append(a)
    return saida


def bate(a, cob) -> bool:
    txt = normalizar(a.descricao + " " + a.plano)
    pal = [normalizar(p) for p in cob.get("palavras", [])]
    fora = [normalizar(p) for p in cob.get("excluir", [])]
    return (not pal or any(p in txt for p in pal)) and not any(f in txt for f in fora)


def prazo_dt(cob, dia: date) -> datetime:
    t = datetime.combine(dia, hora(cob["prazo"]))
    return t + timedelta(days=1) if vira_meia_noite(cob) else t


def semaforo(cob, dia: date) -> tuple[str, str]:
    agora = datetime.now(FUSO)
    limite = prazo_dt(cob, dia)
    falta = (limite - agora).total_seconds() / 60
    if falta < 0:
        return "🔴", "VERIFICAR - POSSÍVEL ATRASO"
    if falta <= 60:
        return "🟡", f"vence em {int(falta)} min"
    return "🟢", "no prazo"


def bloco_atividades(ativs, cads, limite=25) -> list[str]:
    linhas = [f"• {esc(legivel(a.descricao))}" for a in ativs[:limite]]
    if len(ativs) > limite:
        linhas.append(f"• … e mais {len(ativs) - limite}")
    if CFG.get("mostrar_executores", False):
        execs = sorted({e for a in ativs for e in a.executores})
        if execs:
            marcados = [mencao(e, cads) for e in execs
                        if any(normalizar(c["nome_leankeep"]) == normalizar(e) for c in cads.values())]
            txt = esc(resumir_executores(execs))
            linhas.append(f"   <i>Executores: {txt}</i>" + (f"  {' '.join(marcados)}" if marcados else ""))
    return linhas


def lista_por_cobranca(ativs, dia: date, cads) -> list[str]:
    """Agrupa as atividades pela cobrança (Leituras, Banheiros…), com semáforo de prazo."""
    restantes = sem_duplicadas(ativs)
    linhas = []
    for cob in CFG.get("cobrancas", []):
        if not roda_hoje(cob, dia):
            continue
        do_grupo = [a for a in restantes if bate(a, cob)]
        if not do_grupo:
            continue
        restantes = [a for a in restantes if a not in do_grupo]
        cor, situacao = semaforo(cob, dia)
        linhas += ["", f"<b>{esc(cob['nome'])}</b> ({len(do_grupo)})",
                   f"Prazo {esc(txt_prazo(cob))} – {situacao}"]
        linhas += bloco_atividades(do_grupo, cads)
    if restantes:
        linhas += ["", f"<b>Outras atividades</b> ({len(restantes)})"]
        linhas += bloco_atividades(restantes, cads)
    return linhas


# ---------------- cobrança ----------------
async def cobrar(context: ContextTypes.DEFAULT_TYPE):
    job = context.job.data
    cob, rodada = job["cobranca"], job["rodada"]  # rodada 0 = aviso; 1 = no prazo; 2.. = reforços
    if MODO == "leankeep":
        await cobrar_leankeep(context, cob, rodada)
    else:
        await cobrar_manual(context, cob, rodada)


async def cobrar_leankeep(context, cob, rodada):
    bot = context.bot
    try:
        ativs = await buscar_pendentes(cob.get("palavras"), data_ref(cob), cob.get("excluir"))
    except Exception as e:
        log.exception("Erro ao consultar Leankeep")
        await enviar(bot, CFG.get("gestor_chat_id"),
                     f"⚠️ Robô não conseguiu consultar o Leankeep para "
                     f"<b>{esc(cob['nome'])}</b>: {esc(e)}")
        return

    if not ativs:
        if rodada == 1:  # no horário do prazo, confirma que está tudo em dia
            await enviar_grupo(bot,
                         f"✅ <b>{esc(cob['nome'])}</b>: tudo baixado no Leankeep. Valeu, equipe!")
        return

    cads = cadastros()
    if rodada <= 0:
        falta = f"⏳ Faltam {CFG.get('aviso_antes_prazo_min', 30)} min! " if rodada < 0 else ""
        topo = (f"{falta}⏰ <b>{esc(cob['nome'])}</b> — prazo às <b>{esc(txt_prazo(cob))}</b>\n"
                f"Ainda falta baixar no Leankeep:")
    else:
        topo = (f"🚨 <b>{esc(cob['nome'])}</b> — PRAZO VENCIDO ({esc(txt_prazo(cob))})\n"
                f"Continua pendente no Leankeep:")
    ativs = sem_duplicadas(ativs)
    cor, situacao = semaforo(cob, data_ref(cob))
    linhas = [topo.replace("Ainda falta", f"{cor} {len(ativs)} atividade(s) — ainda falta")
                  .replace("Continua pendente", f"🔴 {len(ativs)} atividade(s) — continua pendente")]
    linhas += bloco_atividades(ativs, cads, limite=30)
    if cob.get("marcar"):
        linhas += ["", marcacoes(cob["marcar"])]
    texto = "\n".join(linhas)
    m = await enviar_grupo(bot, texto, reply_markup=botoes_baixa(cob))
    if m is not None:
        MSG_COBRANCA[m.message_id] = CFG["cobrancas"].index(cob)
    if CFG.get("copiar_cobrancas_gestor", True) and CFG.get("gestor_chat_id") and not TESTE.get():
        await enviar(bot, CFG["gestor_chat_id"], texto)
    if rodada == 1 and not TESTE.get():
        registrar_atrasadas(cob, data_ref(cob), ativs)

    # mensagem no privado de cada executor cadastrado
    if cob.get("privado", True):
        for tid, c in cads.items():
            minhas = [a for a in ativs
                      if any(normalizar(e) == normalizar(c["nome_leankeep"]) for e in a.executores)]
            if not minhas:
                continue
            txt = [f"{'⏰' if rodada <= 0 else '🚨'} Oi {esc(c['nome'])}! "
                   f"Você tem {len(minhas)} atividade(s) de <b>{esc(cob['nome'])}</b> "
                   f"para baixar no Leankeep até {esc(txt_prazo(cob))}:"]
            txt += [f"   ▫️ {esc(legivel(a.descricao))}" for a in minhas[:30]]
            await enviar(bot, int(tid), "\n".join(txt))


async def cobrar_manual(context, cob, rodada):
    feitos = ler(ARQ_FEITOS, {})
    dia = data_ref(cob).isoformat()
    chave = f"{dia}|{cob['nome']}"
    if chave in feitos:
        return
    if rodada <= 0:
        falta = f"⏳ Faltam {CFG.get('aviso_antes_prazo_min', 30)} min! " if rodada < 0 else ""
        txt = (f"{falta}⏰ <b>{esc(cob['nome'])}</b> — prazo às <b>{esc(txt_prazo(cob))}</b>\n"
               f"Executou e baixou no Leankeep? Toque no botão abaixo.")
    else:
        txt = (f"🚨 <b>{esc(cob['nome'])}</b> — PRAZO VENCIDO ({esc(txt_prazo(cob))})\n"
               f"Ninguém confirmou ainda. Baixe no Leankeep e toque no botão.")
    resp = cob.get("responsaveis") or []
    if resp:
        txt += "\nResponsáveis: " + ", ".join(esc(r) for r in resp)
    if cob.get("marcar"):
        txt += "\n\n" + marcacoes(cob["marcar"])
    botao = InlineKeyboardMarkup([[InlineKeyboardButton(
        "✅ Feito e baixado", callback_data=f"feito|{dia}|{CFG['cobrancas'].index(cob)}")]])
    await enviar_grupo(context.bot, txt, reply_markup=botao)


async def botao_feito(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        _, dia, idx = q.data.split("|", 2)
        nome = CFG["cobrancas"][int(idx)]["nome"]
    except Exception:
        await q.answer("Este lembrete é antigo.")
        return
    feitos = ler(ARQ_FEITOS, {})
    chave = f"{dia}|{nome}"
    agora = datetime.now(FUSO).strftime("%H:%M")
    quem = q.from_user.full_name
    if chave not in feitos:
        feitos[chave] = {"por": quem, "as": agora}
        gravar(ARQ_FEITOS, feitos)
    await q.answer("Registrado, obrigado!")
    try:
        await q.edit_message_text(
            f"✅ <b>{esc(nome)}</b> confirmado por {esc(feitos[chave]['por'])} "
            f"às {esc(feitos[chave]['as'])}", parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ---------------- resumo diário ----------------
async def resumo(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    # resumo de manhã (antes das 12h) fala do dia anterior; à noite, do próprio dia
    dia = hoje() - timedelta(days=1) if hm(CFG.get("resumo_diario", "23:00")) < (12, 0) else hoje()
    d = dia.strftime("%d/%m/%Y")
    if MODO == "leankeep":
        try:
            ativs = await buscar_pendentes(None, dia)
        except Exception as e:
            await enviar(bot, CFG.get("gestor_chat_id"), f"⚠️ Resumo: erro no Leankeep: {esc(e)}")
            return
        if not ativs:
            txt = f"📊 <b>Resumo {d}</b> — {esc(CFG.get('unidade', ''))}\n✅ Nenhuma atividade pendente."
        else:
            unicas = sem_duplicadas(ativs)
            linhas = [f"📊 <b>Resumo {d}</b> — {esc(CFG.get('unidade', ''))}",
                      f"❌ <b>{len(unicas)}</b> atividade(s) não baixadas:"]
            linhas += lista_por_cobranca(unicas, dia, cadastros())
            txt = "\n".join(linhas)
    else:
        feitos = ler(ARQ_FEITOS, {})
        linhas = [f"📊 <b>Resumo {d}</b>"]
        for cob in CFG.get("cobrancas", []):
            if not roda_hoje(cob, dia):
                continue
            f = feitos.get(f"{dia.isoformat()}|{cob['nome']}")
            linhas.append(f"✅ {esc(cob['nome'])} — {esc(f['por'])} às {esc(f['as'])}" if f
                          else f"❌ {esc(cob['nome'])} — não confirmado")
        txt = "\n".join(linhas)
    await enviar(bot, CFG.get("gestor_chat_id"), txt)


# ---------------- comandos ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        f"Olá! Sou o robô de cobrança dos checklists do Leankeep.\n\n"
        f"ID deste chat: {chat.id}\n"
        f"ID deste tópico: {update.message.message_thread_id if update.message.is_topic_message else '(não é tópico)'}\n"
        f"Seu ID: {user.id}\nModo: {MODO}\n\n"
        f"Gestor: /sou_gestor (no privado) e /configurar_topico (dentro do tópico)\n"
        f"Colaborador: envie /cadastrar Seu Nome Igual ao Leankeep\n"
        f"Ver o que falta agora: /pendentes")


async def cmd_cadastrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nome = " ".join(context.args).strip()
    if not nome:
        await update.message.reply_text(
            "Use assim: /cadastrar João da Silva (o nome exatamente como aparece no Leankeep)")
        return
    cads = cadastros()
    u = update.effective_user
    cads[str(u.id)] = {"nome_leankeep": nome, "nome": u.first_name or nome}
    gravar(ARQ_CADASTROS, cads)
    await update.message.reply_text(
        f"Cadastrado: {nome}. Vou te avisar no privado das suas atividades pendentes.\n"
        f"(Se você fez isso no grupo, abra uma conversa comigo e envie /start, "
        f"senão o Telegram não me deixa te mandar mensagem.)")


async def cmd_pendentes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MODO != "leankeep":
        await update.message.reply_text("Modo manual: não tenho acesso ao Leankeep ainda.")
        return
    await update.message.reply_text("Consultando o Leankeep…")
    try:
        ativs = await buscar_pendentes(context.args or None)
    except Exception as e:
        await update.message.reply_text(f"Erro ao consultar o Leankeep: {e}")
        return
    if not ativs:
        await update.message.reply_text("✅ Nada pendente hoje.")
        return
    unicas = sem_duplicadas(ativs)
    agora = datetime.now(FUSO)
    linhas = [f"📋 <b>Pendentes no Leankeep — {agora.strftime('%d/%m %H:%M')}</b>",
              f"{esc(CFG.get('unidade', ''))} · <b>{len(unicas)}</b> atividade(s) em aberto",
              ]
    linhas += lista_por_cobranca(unicas, hoje(), cadastros())
    await enviar(context.bot, update.effective_chat.id, "\n".join(linhas),
                 message_thread_id=update.message.message_thread_id
                 if update.message.is_topic_message else None)


def salvar_ajuste(**valores):
    aj = ler(ARQ_AJUSTES, {})
    aj.update(valores)
    gravar(ARQ_AJUSTES, aj)
    CFG.update(valores)


async def cmd_configurar_topico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if update.effective_chat.type == "private":
        await msg.reply_text("Envie este comando DENTRO do tópico do grupo onde quer receber as cobranças.")
        return
    if CFG.get("gestor_chat_id") and update.effective_user.id != int(CFG["gestor_chat_id"]):
        await msg.reply_text("Só o gestor pode mudar o local das cobranças.")
        return
    topico = msg.message_thread_id if msg.is_topic_message else 0
    salvar_ajuste(grupo_chat_id=update.effective_chat.id, topico_id=topico)
    onde = "neste tópico" if topico else "neste grupo"
    await msg.reply_text(f"✅ Pronto! As cobranças serão enviadas {onde}.")


async def cmd_sou_gestor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Envie /sou_gestor no meu privado (conversa direta comigo).")
        return
    atual = CFG.get("gestor_chat_id")
    if atual and int(atual) != update.effective_user.id:
        await update.message.reply_text("Já existe um gestor cadastrado.")
        return
    salvar_ajuste(gestor_chat_id=update.effective_user.id)
    await update.message.reply_text("✅ Você foi cadastrado como gestor: vai receber o resumo diário e os avisos de erro aqui.")


async def cmd_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    linhas = [f"🗓️ Cobranças configuradas (modo {MODO}):"]
    for cob in CFG.get("cobrancas", []):
        linhas.append(f"• {cob['nome']}: aviso {cob['aviso']}, prazo {cob['prazo']} "
                      f"({', '.join(cob.get('dias', DIAS.keys()))})")
    linhas.append(f"Resumo diário: {CFG.get('resumo_diario')}")
    linhas.append(f"Grupo: {CFG.get('grupo_chat_id') or 'NÃO CONFIGURADO (/configurar_topico)'}"
                  f" | tópico: {CFG.get('topico_id') or '-'}")
    linhas.append(f"Gestor: {CFG.get('gestor_chat_id') or 'NÃO CONFIGURADO (/sou_gestor)'}")
    linhas.append(f"Ocorrências: {'tópico configurado' if CFG.get('oc_grupo_chat_id') else 'NÃO CONFIGURADO (/configurar_ocorrencias)'}"
                  f" | lista às {oc_cfg().get('lista_horario', '07:00')}"
                  f" | lista e avisos: {'tópico Keepbot' if saida_separada() else 'mesmo tópico (/configurar_keepbot)'}")
    linhas.append(f"Tagme: {'ligada' if TG else 'desligada (TAGME_EMAIL/TAGME_SENHA no .env)'}"
                  f" | destino: {'tópico configurado' if CFG.get('tagme_grupo_chat_id') else 'NÃO CONFIGURADO (/configurar_tagme)'}"
                  f" | reservas às {tg_cfg().get('resumo_reservas', '10:00')}")
    linhas.append(f"Lembrete dos relógios: "
                  f"{'grupo de fechamento configurado' if CFG.get('fech_grupo_chat_id') else 'NÃO CONFIGURADO (/configurar_fechamento)'}"
                  f" | todo dia às {rel_cfg().get('horario', '23:00')}")
    await update.message.reply_text("\n".join(linhas))


# ---------------- agenda dos horários ----------------
def roda_hoje(cob, dia: date | None = None) -> bool:
    return (dia or hoje()).weekday() in [DIAS[d] for d in cob.get("dias", DIAS.keys())]


def agendar(app: Application):
    jq = app.job_queue
    ref = CFG.get("reforco", {})
    for cob in CFG.get("cobrancas", []):
        intervalo = int(cob.get("reforco_intervalo_min", ref.get("intervalo_min", 30)))
        maximo = int(cob.get("reforco_maximo", ref.get("maximo", 3)))
        dias = [DIAS[d] for d in cob.get("dias", DIAS.keys())]
        horarios = [(hora(cob["aviso"]), 0)]
        prazo = datetime.combine(date.today(), hora(cob["prazo"]))
        antes = int(cob.get("aviso_antes_prazo_min", CFG.get("aviso_antes_prazo_min", 0)))
        if antes:
            t2 = (prazo - timedelta(minutes=antes)).timetz()
            if (t2.hour, t2.minute) != hm(cob["aviso"]):
                horarios.append((t2, -1))
        for k in range(maximo + 1):
            t = (prazo + timedelta(minutes=intervalo * k)).timetz()
            horarios.append((t, k + 1))
        for t, rodada in horarios:
            # horários depois da meia-noite caem no dia seguinte
            depois_meia_noite = (t.hour, t.minute) < hm(cob["aviso"])
            dias_job = [(d + 1) % 7 if depois_meia_noite else d for d in dias]
            # python-telegram-bot usa 0=domingo … 6=sábado
            dias_ptb = tuple(sorted((d + 1) % 7 for d in dias_job))
            jq.run_daily(cobrar, t, days=dias_ptb,
                         data={"cobranca": cob, "rodada": rodada},
                         name=f"{cob['nome']}#{rodada}",
                         job_kwargs={"misfire_grace_time": 600, "coalesce": True})
            log.info("Agendado: %s rodada %s às %s", cob["nome"], rodada, t.strftime("%H:%M"))
    ATRASO_OK = {"job_kwargs": {"misfire_grace_time": 3 * 3600, "coalesce": True}}
    for nome, func, h, dias, janela in envios_diarios():
        jq.run_daily(registrar_envio(nome, func), h, days=dias, name=nome, **ATRASO_OK)
    jq.run_repeating(vigiar_atrasos, interval=600, first=60, name="vigiar_atrasos")
    jq.run_repeating(processar_conferencias, interval=60, first=30, name="conferencias")
    if tg_cfg().get("ativo", True):
        jq.run_repeating(vigiar_fila, interval=int(tg_cfg().get("intervalo_fila_seg", 60)),
                         first=20, name="tagme_fila")
    jq.run_once(recuperar_envios, 45, name="recuperar_envios")
    agendar_lembretes(jq)


# ---------------- envios diários que o robô recupera se estava desligado ----------------
ARQ_EXEC = DADOS / "execucoes.json"
TODOS_DIAS = (0, 1, 2, 3, 4, 5, 6)  # python-telegram-bot: 0=domingo


def envios_diarios():
    """(nome, função, horário, dias, janela de recuperação em horas)"""
    lst = []
    if CFG.get("resumo_diario"):
        lst.append(("resumo", resumo, hora(CFG["resumo_diario"]), TODOS_DIAS, 12))
    if oc_cfg().get("ativo", True):
        lst.append(("lista_ocorrencias", oc_publicar_lista, hora(oc_cfg().get("lista_horario", "07:00")),
                    TODOS_DIAS, 12))
    if tg_cfg().get("ativo", True):
        lst.append(("tagme_reservas", resumo_tagme, hora(tg_cfg().get("resumo_reservas", "10:00")),
                    TODOS_DIAS, 10))
    if rel_cfg().get("ativo", True):
        lst.append(("lembrete_relogio", lembrete_relogio, hora(rel_cfg().get("horario", "23:00")),
                    TODOS_DIAS, 1))
    if sem_cfg().get("ativo", True):
        dia_txt, hora_txt = str(sem_cfg().get("quando", "seg 08:00")).split()
        lst.append(("resumo_semanal", enviar_semanal, hora(hora_txt), ((DIAS[dia_txt] + 1) % 7,), 36))
    return lst


def registrar_envio(nome, func):
    async def _job(context: ContextTypes.DEFAULT_TYPE):
        await func(context)
        ex = ler(ARQ_EXEC, {})
        ex[nome] = hoje().isoformat()
        gravar(ARQ_EXEC, ex)
    _job.__name__ = f"job_{nome}"
    return _job


async def recuperar_envios(context: ContextTypes.DEFAULT_TYPE):
    """Ao ligar: manda o que deveria ter saído hoje enquanto o robô estava desligado."""
    agora = datetime.now(FUSO)
    ex = ler(ARQ_EXEC, {})
    recuperados = []
    for nome, func, h, dias, janela in envios_diarios():
        dia_ptb = (agora.weekday() + 1) % 7
        if dia_ptb not in dias:
            continue
        previsto = datetime.combine(agora.date(), h)
        if not (previsto <= agora <= previsto + timedelta(hours=janela)):
            continue
        if ex.get(nome) == agora.date().isoformat():
            continue
        log.info("Recuperando envio perdido: %s (previsto %s)", nome, h.strftime("%H:%M"))
        try:
            await registrar_envio(nome, func)(context)
            recuperados.append(f"{h.strftime('%H:%M')} – {NOMES_ENVIO.get(nome, nome)}")
        except Exception as e:
            log.warning("Falha ao recuperar %s: %s", nome, e)
    if recuperados and CFG.get("gestor_chat_id"):
        await enviar(context.bot, CFG["gestor_chat_id"],
                     "🔁 <b>O robô estava desligado e enviou agora o que tinha ficado para trás:</b>\n"
                     + "\n".join(f"• {r}" for r in recuperados))


NOMES_ENVIO = {"resumo": "resumo do Leankeep", "lista_ocorrencias": "lista de pendências de ocorrências",
               "tagme_reservas": "reservas da Tagme", "lembrete_relogio": "lembrete dos relógios",
               "resumo_semanal": "resumo semanal"}

# =====================================================================
#  OCORRÊNCIAS: fotos/mensagens postadas num tópico viram pendências
# =====================================================================
ARQ_OCORR = DADOS / "ocorrencias.json"


def oc_cfg() -> dict:
    return CFG.get("ocorrencias", {}) or {}


def oc_dados() -> dict:
    return ler(ARQ_OCORR, {"seq": 0, "itens": {}})


def oc_link(o: dict) -> str:
    """Link para a mensagem original no grupo (funciona para quem é membro)."""
    cid = str(o["chat_id"])
    base = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    link = f"https://t.me/c/{base}/{o['msg_ids'][0]}"
    if o.get("topico"):
        link += f"?thread={o['topico']}"
    return link


def eh_resolvido(texto: str) -> bool:
    t = normalizar(texto)
    palavras = oc_cfg().get("palavras_resolvido",
                            ["RESOLVIDO", "RESOLVIDA", "CONCLUIDO", "CONCLUIDA", "FINALIZADO"])
    return any(normalizar(p) in t for p in palavras)


def oc_coleta():
    """Onde a equipe posta as fotos (ex.: tópico Batidão semanal)."""
    return CFG.get("oc_grupo_chat_id"), (CFG.get("oc_topico_id") or 0)


def oc_saida():
    """Onde o robô publica a lista, as confirmações e os avisos (ex.: tópico Keepbot).
    Sem tópico próprio configurado, usa o mesmo tópico da coleta."""
    if CFG.get("oc_saida_chat_id"):
        return CFG["oc_saida_chat_id"], (CFG.get("oc_saida_topico_id") or 0)
    return oc_coleta()


def saida_separada() -> bool:
    c1, t1 = oc_coleta()
    c2, t2 = oc_saida()
    return bool(c2) and (str(c1), int(t1 or 0)) != (str(c2), int(t2 or 0))


async def oc_avisar(bot, msg, texto, **kw):
    """Responde sobre uma ocorrência: no tópico Keepbot (se configurado) ou como resposta à foto."""
    if saida_separada():
        c, t = oc_saida()
        return await enviar(bot, c, texto, message_thread_id=t or None, **kw)
    return await msg.reply_text(texto, **kw)


def no_topico_saida(msg) -> bool:
    if not saida_separada():
        return False
    c, t = oc_saida()
    atual = msg.message_thread_id if msg.is_topic_message else 0
    return msg.chat.id == int(c) and int(t or 0) == int(atual or 0)


def no_topico_ocorrencias(msg) -> bool:
    gid = CFG.get("oc_grupo_chat_id")
    if not gid or msg.chat.id != int(gid):
        return False
    top = CFG.get("oc_topico_id") or 0
    atual = msg.message_thread_id if msg.is_topic_message else 0
    return int(top) == int(atual or 0)


def oc_resolver(dados: dict, num: str, quem: str) -> dict | None:
    o = dados["itens"].get(str(num))
    if not o or o.get("resolvida"):
        return None
    o["resolvida"] = {"por": quem, "em": datetime.now(FUSO).isoformat(timespec="minutes")}
    gravar(ARQ_OCORR, dados)
    return o


def oc_reativar(dados: dict, num: str, quem: str) -> dict | None:
    o = dados["itens"].get(str(num))
    if not o or not o.get("resolvida"):
        return None
    o.setdefault("historico", []).append({"reativada_por": quem, "em": datetime.now(FUSO).isoformat(timespec="minutes"),
                                          "baixa_anterior": o["resolvida"]})
    o["resolvida"] = None
    o["andamento"] = None
    gravar(ARQ_OCORR, dados)
    return o


def _duracao(ini: datetime, fim: datetime) -> str:
    min_tot = max(0, int((fim - ini).total_seconds() // 60))
    d, h, m = min_tot // 1440, (min_tot % 1440) // 60, min_tot % 60
    if d:
        return f"{d} dia(s) e {h}h"
    return f"{h}h{m:02d}" if h else f"{m} min"


async def oc_baixa_privado(bot, num):
    """Quando uma ocorrência é baixada, manda no privado do gestor: foto, quem baixou e quando."""
    g = CFG.get("gestor_chat_id")
    if not g or not oc_cfg().get("avisar_baixa_privado", True):
        return
    o = oc_dados()["itens"].get(str(num))
    if not o or not o.get("resolvida"):
        return
    r = o["resolvida"]
    criada = datetime.fromisoformat(o["criada"])
    baixa = datetime.fromisoformat(r["em"])
    if o.get("foto"):
        try:
            await bot.copy_message(int(g), o["chat_id"], o["msg_ids"][0])
        except Exception as e:
            log.warning("Não consegui copiar a foto da ocorrência #%s: %s", num, e)
    reat = len(o.get("historico", []))
    L = [f"✅ <b>Ocorrência #{num} baixada</b>", "",
         esc(o.get("texto") or "(só foto)"), "",
         f"• Baixada por: <b>{esc(r['por'])}</b>",
         f"• Data da baixa: <b>{baixa:%d/%m/%Y às %H:%M}</b>",
         f"• Registrada por: {esc(o.get('autor', '?'))} em {criada:%d/%m/%Y às %H:%M}",
         f"• Tempo até a baixa: {_duracao(criada, baixa)}"]
    if reat:
        L.append(f"• Já foi reativada {reat} vez(es) antes")
    L.append(f"• <a href=\"{oc_link(o)}\">Ver a mensagem original no grupo</a>")
    L += ["", "<i>Baixa feita por engano? Toque em Reativar.</i>"]
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton(f"↩️ Reativar #{num}", callback_data=f"ocr|{num}")]])
    await enviar(bot, int(g), "\n".join(L), reply_markup=teclado)


async def oc_avisar_reativada(bot, num, quem):
    o = oc_dados()["itens"].get(str(num))
    if not o:
        return
    c, t = oc_saida()
    link = f"<a href=\"{oc_link(o)}\">{'📷 ver foto' if o.get('foto') else '↗ ver mensagem'}</a>"
    resumo = esc((o.get("texto") or "(só foto)")[:120])
    await enviar(bot, c, f"↩️ <b>Ocorrência #{num} reativada</b> – volta para 🔴 Pendente\n\n"
                         f"{resumo}\n<i>Reativada por {esc(quem)} · {datetime.now(FUSO):%d/%m %H:%M}</i> · {link}",
                 message_thread_id=t or None, reply_markup=botao_oc(num, o))


async def botao_reativar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    g = CFG.get("gestor_chat_id")
    if g and q.from_user.id != int(g):
        await q.answer("Só o gestor pode reativar.", show_alert=True)
        return
    num = q.data.split("|", 1)[1]
    o = oc_reativar(oc_dados(), num, q.from_user.full_name)
    if not o:
        await q.answer("Essa ocorrência já está em aberto.", show_alert=True)
        return
    await q.answer(f"#{num} reativada")
    try:
        await q.edit_message_text(limpar_texto((q.message.text_html or q.message.text or "")
                                               + f"\n\n<b>Reativada às {datetime.now(FUSO):%H:%M} – voltou para Pendente.</b>"),
                                  parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        pass
    await oc_avisar_reativada(context.bot, num, q.from_user.full_name)


async def cmd_reativar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _eh_gestor(update):
        await update.message.reply_text("Só o gestor pode reativar ocorrências.")
        return
    if not context.args:
        await update.message.reply_text("Use assim: /reativar 12")
        return
    ok = []
    for n in [a.lstrip("#") for a in context.args]:
        if oc_reativar(oc_dados(), n, update.effective_user.full_name):
            ok.append(n)
            await oc_avisar_reativada(context.bot, n, update.effective_user.full_name)
    await update.message.reply_text(f"↩️ Reativada(s): {', '.join('#' + n for n in ok)}" if ok
                                    else "Nenhuma ocorrência baixada com esse número.")


def oc_andamento(dados: dict, num: str, quem: str) -> dict | None:
    o = dados["itens"].get(str(num))
    if not o or o.get("resolvida") or o.get("andamento"):
        return None
    o["andamento"] = {"por": quem, "em": datetime.now(FUSO).isoformat(timespec="minutes")}
    gravar(ARQ_OCORR, dados)
    return o


def oc_situacao(o: dict) -> str:
    if o.get("resolvida"):
        return "✅ Concluído"
    if o.get("andamento"):
        return "🟡 Em andamento"
    return "🔴 Pendente"


def botao_oc(num, o: dict | None = None) -> InlineKeyboardMarkup | None:
    if o and o.get("resolvida"):
        return None
    linha = []
    if not (o and o.get("andamento")):
        linha.append(InlineKeyboardButton("🟡 Em andamento", callback_data=f"oca|{num}"))
    linha.append(InlineKeyboardButton("✅ Concluído", callback_data=f"oc|{num}"))
    return InlineKeyboardMarkup([linha])


def eh_andamento(texto: str) -> bool:
    t = normalizar(texto)
    return any(normalizar(p) in t for p in oc_cfg().get(
        "palavras_andamento", ["em andamento", "andamento", "verificando", "vou verificar", "estou vendo"]))


async def oc_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not (no_topico_ocorrencias(msg) or no_topico_saida(msg)):
        return
    texto = (msg.text or msg.caption or "").strip()
    quem = msg.from_user.full_name if msg.from_user else "?"
    dados = oc_dados()

    # 1) resposta a uma ocorrência: "resolvido" fecha a pendência
    alvo = msg.reply_to_message
    if alvo and alvo.message_id != msg.message_thread_id:
        num = None
        for n, o in dados["itens"].items():
            if alvo.message_id in o["msg_ids"] or alvo.message_id == o.get("msg_robo"):
                num = n
                break
        if num and eh_resolvido(texto):
            if oc_resolver(dados, num, quem):
                await oc_avisar(context.bot, msg, f"✅ Ocorrência #{num} concluída por {esc(quem)}.")
                await oc_baixa_privado(context.bot, num)
        elif num and eh_andamento(texto):
            if oc_andamento(dados, num, quem):
                await oc_avisar(context.bot, msg, f"🟡 Ocorrência #{num} em andamento ({esc(quem)}).",
                                reply_markup=botao_oc(num, dados["itens"][num]))
        return  # outras respostas são só conversa sobre a ocorrência
    if no_topico_saida(msg):
        return  # no tópico Keepbot só valem respostas às mensagens do robô

    # 2) álbum de fotos: as demais fotos entram na mesma ocorrência
    if msg.media_group_id:
        for o in dados["itens"].values():
            if o.get("album") == msg.media_group_id:
                o["msg_ids"].append(msg.message_id)
                if texto and not o["texto"]:
                    o["texto"] = texto
                gravar(ARQ_OCORR, dados)
                return

    tem_midia = bool(msg.photo or msg.video or msg.document)
    enviada = msg.date.astimezone(FUSO) if msg.date else datetime.now(FUSO)

    # 3) foto + texto mandados juntos pela mesma pessoa = uma ocorrência só
    janela = int(oc_cfg().get("juntar_segundos", 120))
    uid = msg.from_user.id if msg.from_user else None
    for o in sorted(dados["itens"].values(), key=lambda x: -x["num"]):
        if o.get("resolvida") or o.get("autor_id") != uid or o["chat_id"] != msg.chat.id:
            continue
        ultima = datetime.fromisoformat(o.get("ultima_msg", o["criada"]))
        if abs((enviada - ultima).total_seconds()) <= janela:
            o["msg_ids"].append(msg.message_id)
            if texto:
                o["texto"] = (o["texto"] + " " + texto).strip() if o["texto"] else texto
            if msg.photo:
                o["foto"] = True
                o["msg_ids"].remove(msg.message_id)
                o["msg_ids"].insert(0, msg.message_id)  # link da lista abre a foto
            o["ultima_msg"] = enviada.isoformat(timespec="seconds")
            gravar(ARQ_OCORR, dados)
            return
        break  # só junta com a ocorrência mais recente dessa pessoa

    if not tem_midia and len(texto) < int(oc_cfg().get("min_caracteres_texto", 15)):
        return  # "bom dia", "ok"… não viram ocorrência
    if eh_resolvido(texto) and not tem_midia:
        return

    dados["seq"] += 1
    num = str(dados["seq"])
    dados["itens"][num] = {
        "num": int(num), "chat_id": msg.chat.id,
        "topico": msg.message_thread_id if msg.is_topic_message else 0,
        "msg_ids": [msg.message_id], "album": msg.media_group_id,
        "autor": quem, "autor_id": uid, "texto": texto, "foto": bool(msg.photo),
        "criada": enviada.isoformat(timespec="minutes"),
        "ultima_msg": enviada.isoformat(timespec="seconds"), "resolvida": None,
    }
    gravar(ARQ_OCORR, dados)
    if oc_cfg().get("confirmar_registro", True):
        if saida_separada():
            o = dados["itens"][num]
            resumo = esc(texto[:120] + ("…" if len(texto) > 120 else "")) if texto else "(só foto)"
            link = f"<a href=\"{oc_link(o)}\">{'📷 ver foto' if o.get('foto') else '↗ ver mensagem'}</a>"
            c, t = oc_saida()
            r = await enviar(context.bot, c,
                             f"📌 <b>Ocorrência #{num} registrada</b> – 🔴 Pendente\n\n"
                             f"{resumo}\n"
                             f"<i>{esc(quem)} · {enviada.strftime('%d/%m %H:%M')}</i> · {link}\n\n"
                             f"Use os botões abaixo para atualizar.",
                             message_thread_id=t or None, reply_markup=botao_oc(num))
        else:
            r = await msg.reply_text(
                f"📌 Ocorrência #{num} registrada – 🔴 Pendente\n\n"
                f"Use os botões abaixo (ou responda a foto com \"em andamento\" ou \"resolvido\").",
                reply_markup=botao_oc(num))
        if r is not None:
            dados = oc_dados()
            dados["itens"][num]["msg_robo"] = r.message_id
            gravar(ARQ_OCORR, dados)


async def oc_botao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tipo, num = q.data.split("|", 1)
    quem = q.from_user.full_name
    dados = oc_dados()
    if tipo == "oca":
        o = oc_andamento(dados, num, quem)
        if not o:
            await q.answer("Essa ocorrência já está em andamento ou concluída.")
            return
        await q.answer(f"#{num} em andamento")
        aviso = f"🟡 Ocorrência #{num} em andamento – {esc(quem)}"
    else:
        o = oc_resolver(dados, num, quem)
        if not o:
            await q.answer("Essa ocorrência já estava concluída.")
            return
        await q.answer(f"#{num} concluída!")
        aviso = f"✅ Ocorrência #{num} concluída – {esc(quem)}"
        await oc_baixa_privado(context.bot, num)
    # atualiza a mensagem onde o botão foi tocado
    try:
        texto_atual = q.message.text or ""
        if texto_atual.startswith("📋"):  # é a lista: redesenha inteira
            texto, teclado = oc_texto_lista()
            await q.edit_message_text(limpar_texto(texto), parse_mode=ParseMode.HTML,
                                      reply_markup=teclado, disable_web_page_preview=True)
        else:  # mensagem de registro da ocorrência
            await q.edit_message_text(f"📌 Ocorrência #{num} – {oc_situacao(o)}\n\n"
                                      + (f"Concluída por {quem}." if o.get("resolvida")
                                         else f"Em andamento com {quem}."),
                                      reply_markup=botao_oc(num, o))
    except Exception:
        pass
    # avisa no tópico Keepbot (ou no de ocorrências), a não ser que o toque já tenha sido
    # na própria mensagem de registro desse tópico
    sc, st = oc_saida()
    m = q.message
    no_registro_da_saida = (sc and m.chat.id == int(sc)
                            and int((m.message_thread_id if m.is_topic_message else 0) or 0) == int(st or 0)
                            and not (m.text or "").startswith("📋"))
    if not no_registro_da_saida:
        await enviar(context.bot, sc or o["chat_id"], aviso, message_thread_id=st or None)


def oc_texto_lista() -> tuple[str, InlineKeyboardMarkup | None]:
    dados = oc_dados()
    agora = datetime.now(FUSO)
    abertas = sorted([o for o in dados["itens"].values() if not o.get("resolvida")], key=lambda o: o["num"])
    ontem = (agora - timedelta(days=1)).date().isoformat()
    concl_ontem = sum(1 for o in dados["itens"].values()
                      if o.get("resolvida") and o["resolvida"]["em"][:10] == ontem)
    novas_ontem = sum(1 for o in dados["itens"].values() if o["criada"][:10] == ontem)
    pend = sum(1 for o in abertas if not o.get("andamento"))
    andam = len(abertas) - pend

    L = [f"📋 <b>Ocorrências em aberto – {agora.strftime('%d/%m')}</b>", ""]
    if not abertas:
        L.append("Nenhuma ocorrência em aberto. ✅")
    else:
        L.append(f"Total: <b>{len(abertas)}</b> · Pendentes 🔴 <b>{pend}</b> · Em andamento 🟡 <b>{andam}</b>")
    L.append(f"Ontem: {plural(novas_ontem, 'nova')}, {plural(concl_ontem, 'concluída')}")
    for o in abertas:
        criada = datetime.fromisoformat(o["criada"])
        dias = (agora.date() - criada.date()).days
        idade = "hoje" if dias == 0 else ("há 1 dia" if dias == 1 else f"há {dias} dias")
        texto = esc(o["texto"][:120] + ("…" if len(o["texto"]) > 120 else "")) or "(só foto)"
        link = f"<a href=\"{oc_link(o)}\">{'📷 ver foto' if o.get('foto') else '↗ ver mensagem'}</a>"
        L += ["", f"<b>#{o['num']}</b> {oc_situacao(o)} · {idade}",
              texto,
              f"<i>{esc(o['autor'])} · {criada.strftime('%d/%m %H:%M')}</i> · {link}"]
        if o.get("andamento"):
            L.append(f"<i>Em andamento com {esc(o['andamento']['por'])}</i>")
    teclado = None
    if abertas:
        linhas_bt = []
        for o in abertas[:45]:
            n = o["num"]
            linha = []
            if not o.get("andamento"):
                linha.append(InlineKeyboardButton(f"#{n} 🟡 Em andamento", callback_data=f"oca|{n}"))
            linha.append(InlineKeyboardButton(f"#{n} ✅ Concluído", callback_data=f"oc|{n}"))
            linhas_bt.append(linha)
        teclado = InlineKeyboardMarkup(linhas_bt)
        L += ["", "<i>Use os botões abaixo para atualizar cada ocorrência.</i>"]
    return "\n".join(L), teclado


async def oc_publicar_lista(context: ContextTypes.DEFAULT_TYPE):
    texto, teclado = oc_texto_lista()
    extra = {"reply_markup": teclado} if teclado else {}
    c, t = oc_saida()
    if c:
        await enviar(context.bot, c, texto, message_thread_id=t or None, **extra)
    if CFG.get("gestor_chat_id"):
        await enviar(context.bot, CFG["gestor_chat_id"], texto, **extra)


async def cmd_configurar_ocorrencias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if update.effective_chat.type == "private":
        await msg.reply_text("Envie este comando DENTRO do tópico de ocorrências do grupo.")
        return
    if CFG.get("gestor_chat_id") and update.effective_user.id != int(CFG["gestor_chat_id"]):
        await msg.reply_text("Só o gestor pode configurar o tópico de ocorrências.")
        return
    topico = msg.message_thread_id if msg.is_topic_message else 0
    salvar_ajuste(oc_grupo_chat_id=update.effective_chat.id, oc_topico_id=topico)
    await msg.reply_text(
        "✅ Pronto! Fotos e mensagens deste tópico agora viram ocorrências numeradas.\n"
        f"Lista de pendências todo dia às {oc_cfg().get('lista_horario', '07:00')}.")


async def cmd_configurar_keepbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if update.effective_chat.type == "private":
        await msg.reply_text("Envie este comando DENTRO do tópico Keepbot do grupo da manutenção.")
        return
    if CFG.get("gestor_chat_id") and update.effective_user.id != int(CFG["gestor_chat_id"]):
        await msg.reply_text("Só o gestor pode configurar o tópico Keepbot.")
        return
    topico = msg.message_thread_id if msg.is_topic_message else 0
    salvar_ajuste(oc_saida_chat_id=update.effective_chat.id, oc_saida_topico_id=topico)
    await msg.reply_text(
        "✅ Pronto! A partir de agora este tópico recebe:\n"
        f"• a lista de ocorrências pendentes todo dia às {oc_cfg().get('lista_horario', '07:00')}\n"
        "• o aviso de cada ocorrência nova, com os botões\n"
        "• os avisos de \"em andamento\" e \"concluída\"\n\n"
        "As fotos continuam sendo postadas no tópico de ocorrências de sempre.")


async def cmd_pendencias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto, teclado = oc_texto_lista()
    msg = update.message
    await enviar(context.bot, update.effective_chat.id, texto,
                 message_thread_id=msg.message_thread_id if msg.is_topic_message else None,
                 **({"reply_markup": teclado} if teclado else {}))


async def cmd_excluir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if CFG.get("gestor_chat_id") and update.effective_user.id != int(CFG["gestor_chat_id"]):
        await update.message.reply_text("Só o gestor pode excluir ocorrências.")
        return
    if not context.args:
        await update.message.reply_text("Use assim: /excluir 12  (apaga a ocorrência da lista)")
        return
    dados = oc_dados()
    apagadas = [a.lstrip("#") for a in context.args if dados["itens"].pop(a.lstrip("#"), None)]
    gravar(ARQ_OCORR, dados)
    await update.message.reply_text(
        f"🗑️ Excluída(s): {', '.join('#' + n for n in apagadas)}" if apagadas
        else "Não encontrei ocorrência com esse número.")


async def cmd_resolver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use assim: /resolver 12")
        return
    nums = [a.lstrip("#") for a in context.args]
    dados = oc_dados()
    ok = [n for n in nums if oc_resolver(dados, n, update.effective_user.full_name)]
    for n in ok:
        await oc_baixa_privado(context.bot, n)
    await update.message.reply_text(
        f"✅ Resolvida(s): {', '.join('#' + n for n in ok)}" if ok
        else "Nenhuma ocorrência em aberto com esse número.")


REDE = {"ipv4": True}   # escolhido no teste de conexão da partida


def conexao(leitura: float = 30) -> HTTPXRequest:
    """Conexão tolerante a internet instável: espera mais e tenta de novo.
    Usa IPv4 forçado ou o modo normal do Windows, conforme o que funcionou na partida."""
    extra = {"local_address": "0.0.0.0"} if REDE["ipv4"] else {}
    return HTTPXRequest(
        connection_pool_size=8, connect_timeout=20, read_timeout=leitura,
        write_timeout=30, pool_timeout=10,
        httpx_kwargs={"transport": httpx.AsyncHTTPTransport(retries=5, **extra)})


def esperar_telegram(token: str):
    """Antes de ligar, testa a conexão com o Telegram dos dois jeitos (IPv4 e normal)
    e fica tentando até conseguir, em vez de cair e reiniciar."""
    import time as _t
    url = f"https://api.telegram.org/bot{token}/getMe"
    modos = [("normal", False), ("IPv4", True), ("normal", False)]
    pref = (os.getenv("TELEGRAM_REDE") or "").strip().lower()
    if pref == "ipv4":
        modos.reverse()
    tentativa = 0
    while True:
        tentativa += 1
        for nome, ipv4 in modos:
            try:
                extra = {"local_address": "0.0.0.0"} if ipv4 else {}
                with httpx.Client(transport=httpx.HTTPTransport(retries=4, **extra),
                                  timeout=httpx.Timeout(15, connect=8)) as c:
                    r = c.get(url)
                if r.status_code == 401:
                    raise SystemExit("O Telegram recusou o TELEGRAM_TOKEN do .env (token inválido ou revogado).")
                dado = r.json()
                if dado.get("ok"):
                    REDE["ipv4"] = ipv4
                    log.info("Conectado ao Telegram (modo %s) como @%s", nome,
                             dado["result"].get("username"))
                    return
                log.warning("Telegram respondeu sem ok (modo %s): %s", nome, r.status_code)
            except SystemExit:
                raise
            except Exception as e:
                log.warning("Sem resposta do Telegram (modo %s, tentativa %s): %s",
                            nome, tentativa, type(e).__name__)
        espera = 5 if tentativa < 20 else 30   # a conexão oscila: tenta de novo logo
        log.warning("Telegram não respondeu. Tentando de novo em %s segundos… "
                    "(confira a internet deste computador)", espera)
        _t.sleep(espera)


async def erro_geral(update: object, context: ContextTypes.DEFAULT_TYPE):
    e = context.error
    if isinstance(e, (TimedOut, NetworkError)):
        log.warning("Internet instável (o robô tenta de novo sozinho): %s", e)
    else:
        log.error("Erro inesperado", exc_info=e)


# =====================================================================
#  CONFERÊNCIA: "baixei" no tópico e aviso de baixa em atraso
# =====================================================================
MSG_COBRANCA: dict[int, int] = {}       # id da mensagem de cobrança -> índice da cobrança
ARQ_ATRASO = DADOS / "em_atraso.json"   # pendências que passaram do prazo, em observação
ULTIMA_CONFERENCIA: dict[int, float] = {}
ARQ_HIST_ATRASO = DADOS / "historico_atrasos.json"  # nº da atividade -> dia (baixas em atraso vistas pelo robô)


def registrar_atrasadas(cob, dia: date, ativs):
    dados = ler(ARQ_ATRASO, {})
    idx = str(CFG["cobrancas"].index(cob))
    dia_s = dia.isoformat()
    grupo = dados.setdefault(dia_s, {}).setdefault(idx, {})
    for a in ativs:
        grupo[a.numero or normalizar(a.descricao)] = a.descricao
    gravar(ARQ_ATRASO, dados)


async def vigiar_atrasos(context: ContextTypes.DEFAULT_TYPE):
    """A cada 10 min: o que estava atrasado e sumiu das pendências foi baixado em atraso."""
    if MODO != "leankeep":
        return
    dados = ler(ARQ_ATRASO, {})
    if not dados:
        return
    agora = datetime.now(FUSO)
    ultima = dados.pop("_ultima", None)
    mudou = False
    for dia_s in list(dados):
        dia = date.fromisoformat(dia_s)
        if (agora.date() - dia).days > 2:  # para de vigiar depois de 2 dias
            del dados[dia_s]
            mudou = True
            continue
        try:
            pend = await buscar_pendentes(None, dia)
        except Exception as e:
            log.warning("Vigiar atrasos: erro no Leankeep: %s", e)
            dados["_ultima"] = ultima
            return
        abertas = {a.numero or normalizar(a.descricao) for a in pend}
        for idx, itens in list(dados[dia_s].items()):
            baixadas = {n: d for n, d in itens.items() if n not in abertas}
            if not baixadas:
                continue
            cob = CFG["cobrancas"][int(idx)] if int(idx) < len(CFG["cobrancas"]) else {"nome": "?", "prazo": "?", "aviso": "00:00"}
            if not cob.get("avisar_baixa_atrasada", True):  # ex.: fechamento: só guarda para o semanal
                hist = ler(ARQ_HIST_ATRASO, {})
                hist.update({n: dia_s for n in baixadas})
                gravar(ARQ_HIST_ATRASO, hist)
                for n in baixadas:
                    del itens[n]
                if not itens:
                    del dados[dia_s][idx]
                continue
            janela = (f"entre {datetime.fromisoformat(ultima).strftime('%H:%M')} e {agora.strftime('%H:%M')}"
                      if ultima else f"até {agora.strftime('%H:%M')}")
            await enviar(context.bot, CFG.get("gestor_chat_id"),
                         texto_baixa_atrasada(cob, dia, janela, baixadas.values()))
            hist = ler(ARQ_HIST_ATRASO, {})
            for n in baixadas:
                hist[n] = dia_s
            gravar(ARQ_HIST_ATRASO, hist)
            for n in baixadas:
                del itens[n]
            if not itens:
                del dados[dia_s][idx]
            mudou = True
        if not dados[dia_s]:
            del dados[dia_s]
    dados["_ultima"] = agora.isoformat(timespec="minutes")
    gravar(ARQ_ATRASO, dados)


def texto_baixa_atrasada(cob, dia: date, janela: str, descricoes) -> str:
    linhas = [f"⚠️ <b>Baixa em atraso</b> — {esc(cob['nome'])}",
              f"Prazo: {esc(txt_prazo(cob))} de {dia.strftime('%d/%m')} · baixada {janela}"]
    linhas += [f"   ▫️ {esc(legivel(d))}" for d in descricoes]
    return "\n".join(linhas)


def eh_aviso_de_baixa(texto: str) -> bool:
    t = f" {normalizar(texto)} "
    palavras = CFG.get("palavras_baixa", ["baixei", "baixado", "baixada", "baixamos", "feito",
                                          "concluido", "concluida", "finalizado", "pronto", "realizado"])
    return any(f" {normalizar(p)}" in t for p in palavras)


def cobrancas_em_andamento():
    """Cobranças cujo 1º aviso já passou (no dia a que se referem)."""
    agora = datetime.now(FUSO)
    saida = []
    for cob in CFG.get("cobrancas", []):
        dia = data_ref(cob)
        if not roda_hoje(cob, dia):
            continue
        inicio = datetime.combine(dia, hora(cob["aviso"]))
        if inicio <= agora:
            saida.append(cob)
    return saida


async def conferir_baixa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    texto = (msg.text or msg.caption or "").strip()
    if MODO != "leankeep" or not eh_aviso_de_baixa(texto):
        return
    alvo = msg.reply_to_message
    if alvo and alvo.message_id in MSG_COBRANCA:
        cobs = [CFG["cobrancas"][MSG_COBRANCA[alvo.message_id]]]
    else:
        cobs = cobrancas_em_andamento()
    if not cobs:
        return
    u = msg.from_user
    await agendar_conferencia(context.bot, cobs, u, msg.chat.id,
                              msg.message_thread_id if msg.is_topic_message else None,
                              msg.message_id, origem="texto")


# ---------------------------------------------------------------------
#  Conferência com espera: o Leankeep às vezes demora a sincronizar a baixa.
#  Quem avisa "baixei" (ou toca em "Baixa realizada") recebe um "anotado";
#  depois de espera_conferencia_min minutos o robô confere:
#    - tudo baixado  -> ✅ no grupo
#    - ainda falta   -> ⚠️ SOMENTE no privado do gestor
# ---------------------------------------------------------------------
ARQ_CONF_AGENDADAS = DADOS / "conferencias_agendadas.json"


def espera_conferencia() -> int:
    return int(CFG.get("espera_conferencia_min", 15))


async def agendar_conferencia(bot, cobs, u, chat_id, topico, msg_id, origem: str):
    agora = datetime.now(FUSO)
    idxs = sorted(CFG["cobrancas"].index(c) for c in cobs)
    lst = ler(ARQ_CONF_AGENDADAS, [])
    ja = any(c["chat"] == chat_id and c["cobs"] == idxs for c in lst)
    if not ja:
        lst.append({"cobs": idxs, "dia": {str(i): data_ref(CFG["cobrancas"][i]).isoformat() for i in idxs},
                    "uid": u.id if u else None,
                    "quem": (u.full_name if u else "") or "?",
                    "primeiro": (u.first_name if u else "") or "",
                    "chat": chat_id, "topico": topico, "msg": msg_id, "origem": origem,
                    "marcado_em": agora.isoformat(timespec="minutes"),
                    "conferir_em": (agora + timedelta(minutes=espera_conferencia())).isoformat(timespec="seconds"),
                    "tentativas": 0})
        gravar(ARQ_CONF_AGENDADAS, lst)
    nomes = ", ".join(esc(c["nome"]) for c in cobs)
    quem = _mencao_usuario(u) if u else ""
    await enviar(bot, chat_id,
                 f"📝 Anotado, {quem}! <b>{nomes}</b>\n"
                 f"Vou conferir no Leankeep daqui a uns {espera_conferencia()} min "
                 f"(o app às vezes demora para sincronizar).",
                 message_thread_id=topico, reply_to_message_id=msg_id)


async def processar_conferencias(context: ContextTypes.DEFAULT_TYPE):
    """Confere as baixas agendadas cujo tempo de espera já passou."""
    if MODO != "leankeep":
        return
    lst = ler(ARQ_CONF_AGENDADAS, [])
    if not lst:
        return
    agora = datetime.now(FUSO)
    g = CFG.get("gestor_chat_id")
    restantes = []
    for c in lst:
        if datetime.fromisoformat(c["conferir_em"]) > agora:
            restantes.append(c)
            continue
        cobs = [(CFG["cobrancas"][i], date.fromisoformat(c["dia"][str(i)]))
                for i in c["cobs"] if i < len(CFG["cobrancas"])]
        faltando, erro = [], None
        for cob, dia in cobs:
            try:
                ativs = sem_duplicadas(await buscar_pendentes(cob.get("palavras"), dia, cob.get("excluir")))
            except Exception as e:
                erro = e
                break
            if ativs:
                faltando.append((cob, dia, ativs))
        if erro is not None:
            c["tentativas"] = c.get("tentativas", 0) + 1
            if c["tentativas"] < 4:  # tenta de novo nas próximas rodadas
                restantes.append(c)
            elif g:
                await enviar(context.bot, int(g),
                             f"⚠️ Não consegui conferir no Leankeep a baixa marcada por "
                             f"<b>{esc(c['quem'])}</b> ({', '.join(esc(cob['nome']) for cob, _ in cobs)}): {esc(erro)}")
            continue
        quem_txt = c.get("primeiro") or c["quem"]
        if not faltando:
            for cob, _ in cobs:
                if c["origem"] == "botao":
                    registrar_resposta(cob, c["quem"], "realizada")
            nomes = ", ".join(esc(cob["nome"]) for cob, _ in cobs)
            await enviar(context.bot, c["chat"],
                         f"✅ Conferi no Leankeep: <b>tudo baixado!</b>\n\n{nomes}\n\nValeu, {esc(quem_txt)}!",
                         message_thread_id=c.get("topico"), reply_to_message_id=c.get("msg"))
            continue
        # pendência: só no privado do gestor
        for cob, _, _ in faltando:
            if c["origem"] == "botao":
                registrar_resposta(cob, c["quem"], "realizada_mas_pendente")
        if not g:
            log.warning("Conferência com pendência, mas o gestor não está configurado (/sou_gestor)")
            continue
        marcado = datetime.fromisoformat(c["marcado_em"]).strftime("%H:%M")
        await enviar(context.bot, int(g), texto_pendencia_privado(c["quem"], marcado, faltando))
    gravar(ARQ_CONF_AGENDADAS, restantes)


def texto_pendencia_privado(quem: str, marcado: str, faltando) -> str:
    L = ["⚠️ <b>Baixa com pendência no Leankeep</b>", "",
         f"<b>{esc(quem)}</b> marcou como baixado às {marcado}, mas "
         f"{espera_conferencia()} min depois o Leankeep ainda mostra:"]
    for cob, dia, ativs in faltando:
        L += ["", f"<b>{esc(cob['nome'])}</b> — prazo {esc(txt_prazo(cob))} de {dia.strftime('%d/%m')}"]
        L += [f"• {esc(legivel(a.descricao))}" for a in ativs[:20]]
        if len(ativs) > 20:
            L.append(f"• … e mais {len(ativs) - 20}")
    return "\n".join(L)


def texto_tudo_baixado(cobs, quem: str) -> str:
    nomes = ", ".join(c["nome"] for c in cobs)
    return f"✅ Conferi no Leankeep: tudo baixado!\n\n{nomes}\n\nValeu, {quem}!"


def texto_faltam_baixas(quem: str, faltando) -> str:
    linhas = [f"⚠️ <b>{esc(quem)}, ainda faltam baixas no Leankeep</b>"]
    for cob, ativs in faltando:
        _, situacao = semaforo(cob, data_ref(cob))
        linhas += ["", f"<b>{esc(cob['nome'])}</b>",
                   f"Prazo {esc(txt_prazo(cob))} – {situacao}"]
        linhas += [f"• {esc(legivel(a.descricao))}" for a in ativs[:20]]
        if len(ativs) > 20:
            linhas.append(f"• … e mais {len(ativs) - 20}")
    linhas += ["", "<i>Confira se a baixa foi salva no app (às vezes fica sem sincronizar).</i>"]
    return "\n".join(linhas)


def no_topico_leankeep(msg) -> bool:
    gid = CFG.get("grupo_chat_id")
    if not gid or msg.chat.id != int(gid):
        return False
    top = int(CFG.get("topico_id") or 0)
    atual = msg.message_thread_id if msg.is_topic_message else 0
    return top == int(atual or 0)


ARQ_PESSOAS = DADOS / "pessoas.json"


def lembrar_pessoa(u):
    try:
        if not u or getattr(u, "is_bot", False) or not getattr(u, "id", None):
            return
        nome = getattr(u, "full_name", None) or getattr(u, "first_name", "") or ""
        novo = {"nome": nome, "primeiro": getattr(u, "first_name", "") or nome.split(" ")[0],
                "usuario": getattr(u, "username", "") or ""}
        pes = ler(ARQ_PESSOAS, {})
        if not novo["usuario"] and pes.get(str(u.id), {}).get("usuario"):
            novo["usuario"] = pes[str(u.id)]["usuario"]  # não apaga um @ já conhecido
        if pes.get(str(u.id)) != novo:
            pes[str(u.id)] = novo
            gravar(ARQ_PESSOAS, pes)
    except Exception as e:
        log.warning("Não guardei a pessoa: %s", e)


def marcacoes(lista) -> str:
    """'@usuario' vira menção normal; um nome sem @ (ex.: 'Gabrielle') vira menção pelo
    cadastro de quem já escreveu nos grupos — notifica mesmo sem nome de usuário."""
    pes = ler(ARQ_PESSOAS, {})
    out = []
    for m in lista or []:
        m = str(m).strip()
        if m.startswith("@"):
            out.append(esc(m))
            continue
        alvo = normalizar(m)
        achou = next((uid for uid, p in pes.items()
                      if normalizar(p.get("nome", "")) == alvo or normalizar(p.get("primeiro", "")) == alvo), None) \
            or next((uid for uid, p in pes.items() if normalizar(p.get("nome", "")).startswith(alvo)), None)
        out.append(f'<a href="tg://user?id={achou}">{esc(m)}</a>' if achou else esc(m))
    return " ".join(out)


def meus_usuarios() -> set[str]:
    """@ do gestor: o que estiver no config ([avisos] meu_usuario) e o que o robô já viu."""
    us = set()
    cfg = (CFG.get("avisos", {}) or {}).get("meu_usuario", "")
    for u in ([cfg] if isinstance(cfg, str) else cfg):
        if u:
            us.add(u.lstrip("@").lower())
    g = CFG.get("gestor_chat_id")
    if g:
        p = ler(ARQ_PESSOAS, {}).get(str(g), {})
        if p.get("usuario"):
            us.add(p["usuario"].lower())
    return us


async def avisar_mencao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Se alguém marcar o @ do gestor num grupo, manda a mensagem no privado dele."""
    msg = update.effective_message
    g = CFG.get("gestor_chat_id")
    if not msg or not g or not (CFG.get("avisos", {}) or {}).get("mencoes", True):
        return
    if msg.from_user and msg.from_user.id == int(g):
        return
    meus = meus_usuarios()
    marcou = False
    for ent, txt in list(msg.parse_entities().items()) + list(msg.parse_caption_entities().items()):
        if ent.type == "mention" and txt.lstrip("@").lower() in meus:
            marcou = True
        elif ent.type == "text_mention" and ent.user and ent.user.id == int(g):
            marcou = True
    if not marcou:
        return
    quem = msg.from_user.full_name if msg.from_user else "?"
    cid = str(msg.chat.id)
    base = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    link = f"https://t.me/c/{base}/{msg.message_id}"
    if msg.is_topic_message and msg.message_thread_id:
        link += f"?thread={msg.message_thread_id}"
    texto = (msg.text or msg.caption or "").strip()
    quando = msg.date.astimezone(FUSO) if msg.date else datetime.now(FUSO)
    L = [f"🔔 <b>Você foi marcado por {esc(quem)}</b>", "",
         f"• Grupo: {esc(msg.chat.title or '')}",
         f"• Quando: {quando:%d/%m às %H:%M}"]
    if texto:
        L += ["", esc(texto[:600] + ("…" if len(texto) > 600 else ""))]
    L += ["", f"<a href=\"{link}\">Abrir a mensagem no grupo</a>"]
    if msg.photo or msg.video or msg.document:
        try:
            await context.bot.copy_message(int(g), msg.chat.id, msg.message_id)
        except Exception as e:
            log.warning("Não consegui copiar a mensagem marcada: %s", e)
    await enviar(context.bot, int(g), "\n".join(L))


async def guardar_pessoa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lembrar_pessoa(update.effective_user)


async def mensagem_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    lembrar_pessoa(msg.from_user)
    try:
        await avisar_mencao(update, context)
    except Exception:
        log.exception("Erro ao verificar menção")
    if no_topico_ocorrencias(msg) or no_topico_saida(msg):
        await oc_mensagem(update, context)
    elif no_topico_leankeep(msg):
        if not await receber_msg_livre(update, context):
            await conferir_baixa(update, context)


# =====================================================================
#  BOTÕES NAS COBRANÇAS/LEMBRETES: Baixa realizada / Baixa pendente / Escrever mensagem
# =====================================================================
ARQ_RESPOSTAS = DADOS / "respostas_cobranca.json"


def botoes_baixa(cob):
    if not cob or not CFG.get("botoes_baixa", True):
        return None
    i = CFG["cobrancas"].index(cob)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Baixa realizada", callback_data=f"bx|r|{i}"),
         InlineKeyboardButton("⏳ Baixa pendente", callback_data=f"bx|p|{i}")],
        [InlineKeyboardButton("✍️ Escrever mensagem", callback_data=f"bx|m|{i}")]])


def registrar_resposta(cob, quem, tipo, texto=""):
    dados = ler(ARQ_RESPOSTAS, {})
    lst = dados.setdefault("itens", [])
    lst.append({"em": datetime.now(FUSO).isoformat(timespec="minutes"), "cobranca": cob["nome"],
                "quem": quem, "tipo": tipo, "texto": texto})
    dados["itens"] = lst[-500:]
    gravar(ARQ_RESPOSTAS, dados)


def _mencao_usuario(u) -> str:
    return f'<a href="tg://user?id={u.id}">{esc(u.first_name or u.full_name)}</a>'


async def botao_baixa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, acao, idx = q.data.split("|", 2)
    try:
        cob = CFG["cobrancas"][int(idx)]
    except Exception:
        await q.answer("Essa cobrança não existe mais.")
        return
    u = q.from_user
    quem = u.full_name
    chat = q.message.chat.id
    top = q.message.message_thread_id if q.message.is_topic_message else None
    g = CFG.get("gestor_chat_id")

    if acao == "r":
        await q.answer(f"Anotado! Confiro no Leankeep em ~{espera_conferencia()} min.")
        await agendar_conferencia(context.bot, [cob], u, chat, top, q.message.message_id, origem="botao")
    elif acao == "p":
        await q.answer("Anotado: baixa pendente.")
        registrar_resposta(cob, quem, "pendente")
        await enviar(context.bot, chat,
                     f"⏳ <b>{esc(cob['nome'])}</b>\n\n{_mencao_usuario(u)} informou que a baixa está "
                     f"<b>pendente</b>. Prazo: {esc(txt_prazo(cob))}.",
                     message_thread_id=top, reply_to_message_id=q.message.message_id)
        if g and CFG.get("avisar_respostas_gestor", True):
            await enviar(context.bot, int(g), f"⏳ <b>{esc(quem)}</b> informou baixa <b>pendente</b> em "
                                              f"<b>{esc(cob['nome'])}</b> (prazo {esc(txt_prazo(cob))}).")
    elif acao == "m":
        await q.answer("Escreva sua mensagem respondendo ao pedido do robô.")
        m = await enviar(context.bot, chat,
                         f"✍️ {_mencao_usuario(u)}, escreva sua mensagem sobre <b>{esc(cob['nome'])}</b> "
                         f"<b>respondendo a esta mensagem</b>.",
                         message_thread_id=top,
                         reply_markup=ForceReply(selective=True, input_field_placeholder="Sua mensagem…"))
        if m is not None:
            dados = ler(ARQ_RESPOSTAS, {})
            dados.setdefault("pedidos", {})[str(m.message_id)] = {
                "uid": u.id, "cob": int(idx), "em": datetime.now(FUSO).isoformat(timespec="minutes")}
            gravar(ARQ_RESPOSTAS, dados)


async def receber_msg_livre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Resposta ao pedido "escreva sua mensagem": registra, confirma e manda para o gestor."""
    msg = update.effective_message
    alvo = msg.reply_to_message
    if not alvo:
        return False
    dados = ler(ARQ_RESPOSTAS, {})
    ped = dados.get("pedidos", {}).get(str(alvo.message_id))
    if not ped:
        return False
    texto = (msg.text or msg.caption or "").strip()
    if not texto:
        return True
    try:
        cob = CFG["cobrancas"][int(ped["cob"])]
    except Exception:
        return True
    quem = msg.from_user.full_name if msg.from_user else "?"
    dados["pedidos"].pop(str(alvo.message_id), None)
    gravar(ARQ_RESPOSTAS, dados)
    registrar_resposta(cob, quem, "mensagem", texto)
    await enviar(context.bot, msg.chat.id, f"📝 Recebido, {esc(msg.from_user.first_name if msg.from_user else '')}! "
                                           f"Sua mensagem sobre <b>{esc(cob['nome'])}</b> foi registrada.",
                 message_thread_id=msg.message_thread_id if msg.is_topic_message else None,
                 reply_to_message_id=msg.message_id)
    g = CFG.get("gestor_chat_id")
    if g and CFG.get("avisar_respostas_gestor", True):
        await enviar(context.bot, int(g), f"📝 <b>{esc(quem)}</b> sobre <b>{esc(cob['nome'])}</b>:\n\n{esc(texto)}")
    return True


# =====================================================================
#  RESUMO SEMANAL: baixas no prazo x em atraso, por plano e executor
# =====================================================================
ARQ_SEMANAL = DADOS / "semanal.json"


def sem_cfg() -> dict:
    return CFG.get("resumo_semanal", {}) or {}


def _dt(txt) -> datetime | None:
    if not txt:
        return None
    try:
        d = datetime.fromisoformat(str(txt).replace("Z", "+00:00"))
        return d.astimezone(FUSO) if d.tzinfo else d.replace(tzinfo=FUSO)
    except Exception:
        return None


def tolerancia(a) -> timedelta:
    """Tolerância extra depois do horário limite (ex.: gás e exaustão: 3 h). Olha só o PLANO,
    como a planilha (evita pegar, por ex., 'limpeza dos exaustores dos banheiros')."""
    txt = normalizar(a.plano if a.plano and a.plano != "-" else a.descricao)
    for t in sem_cfg().get("tolerancias", [{"palavras": ["REGISTRO GAS", "REGISTRO DE GAS"], "horas": 3},
                                            {"palavras": ["EXAUST"], "horas": 3}]):
        if any(normalizar(p) in txt for p in t.get("palavras", [])):
            return timedelta(hours=float(t.get("horas", 0)))
    return timedelta()


def prazo_atividade(a) -> datetime | None:
    """Data e hora previstas (+ tolerância). Sem hora no Leankeep, usa o prazo da cobrança."""
    base = _prazo_previsto(a)
    return base + tolerancia(a) if base else None


def _prazo_previsto(a) -> datetime | None:
    prev = _dt(a.data)
    if not prev:
        return None
    if (prev.hour, prev.minute) not in ((0, 0), (0, 1)):
        return prev
    for cob in CFG.get("cobrancas", []):
        if bate(a, cob):
            return prazo_dt(cob, prev.date()).replace(tzinfo=FUSO)
    return datetime.combine(prev.date(), time(23, 59), tzinfo=FUSO)


def fora_do_indice(a) -> bool:
    txt = normalizar(a.descricao + " " + a.plano)
    return any(normalizar(p) in txt for p in sem_cfg().get("fora_do_indice", ["FECHAMENTO DA LOJA"]))


def cor_pct(p: float) -> str:
    ok, alerta = sem_cfg().get("meta_atraso_pct", 5), sem_cfg().get("alerta_atraso_pct", 15)
    return "🟢" if p <= ok else ("🟡" if p <= alerta else "🔴")


def pct(a, b) -> float:
    return 100 * a / b if b else 0.0


async def montar_semanal(inicio: date, fim: date) -> str:
    unidade = CFG.get("unidade")
    baixadas = await asyncio.wait_for(asyncio.to_thread(LK.baixadas, inicio, fim, unidade), 300)
    abertas = await asyncio.wait_for(asyncio.to_thread(LK.pendentes, inicio, fim, unidade), 300)
    baixadas, abertas = sem_duplicadas(baixadas), sem_duplicadas(abertas)
    agora = datetime.now(FUSO)
    hist = ler(ARQ_HIST_ATRASO, {})
    tem_hora_baixa = any(a.realizada for a in baixadas)

    linhas_det = []  # (atividade, situacao)
    for a in baixadas:
        limite, feita = prazo_atividade(a), _dt(a.realizada)
        if feita and limite:
            sit = "ATRASADO" if feita > limite else "OK"
        else:
            sit = "ATRASADO" if a.numero in hist else "OK"
        linhas_det.append((a, sit))
    nao_feitas = [a for a in abertas if (prazo_atividade(a) or agora) < agora]

    def conta(itens):
        tot = len(itens)
        atr = sum(1 for _, s in itens if s == "ATRASADO")
        return tot, atr

    indice = [(a, s) for a, s in linhas_det if not fora_do_indice(a)]
    fora = [(a, s) for a, s in linhas_det if fora_do_indice(a)]
    nf_indice = [a for a in nao_feitas if not fora_do_indice(a)]
    tot, atr = conta(indice)
    ok = tot - atr
    previstas = tot + len(nf_indice)
    p_atr = pct(atr, tot)

    # comparação com a semana anterior
    hist_sem = ler(ARQ_SEMANAL, {})
    chave = inicio.isoformat()
    ant = hist_sem.get((inicio - timedelta(days=7)).isoformat())
    hist_sem[chave] = {"total": tot, "atrasadas": atr, "nao_feitas": len(nf_indice), "pct": round(p_atr, 1)}
    gravar(ARQ_SEMANAL, hist_sem)

    L = [f"📊 <b>Resumo semanal — {esc(unidade or '')}</b>",
         f"{inicio.strftime('%d/%m')} a {fim.strftime('%d/%m/%Y')}", ""]
    L.append(f"{cor_pct(p_atr)} <b>Baixas em atraso: {atr} de {tot} ({p_atr:.1f}%)</b>")
    if ant:
        dif = p_atr - ant["pct"]
        seta = "🔺 piorou" if dif > 0.5 else ("🔻 melhorou" if dif < -0.5 else "➖ estável")
        L.append(f"Semana anterior: {ant['pct']:.1f}% · {seta} ({dif:+.1f} p.p.)")
    L.append(f"✅ No prazo: {ok} ({pct(ok, tot):.1f}%)")
    L.append(f"❌ Não realizadas: {len(nf_indice)} de {previstas} previstas")
    if fora:
        f_tot, f_atr = conta(fora)
        L.append(f"<i>Fora do índice (fechamento da loja): {f_atr} de {f_tot} em atraso</i>")

    # por plano
    planos = defaultdict(lambda: [0, 0, 0])
    for a, sit in indice:
        planos[a.plano][0] += 1
        planos[a.plano][1] += sit == "ATRASADO"
    for a in nf_indice:
        planos[a.plano][2] += 1
    L += ["", "<b>Por plano</b> (atraso · não feitas)"]
    for plano, (t, at, nf) in sorted(planos.items(), key=lambda x: (-pct(x[1][1], x[1][0]), -x[1][0])):
        L.append(f"{cor_pct(pct(at, t))} {esc(legivel(plano))}: {at}/{t} ({pct(at, t):.0f}%)"
                 + (f" · ❌ {nf}" if nf else ""))

    # por executor (quem baixou)
    execs = defaultdict(lambda: [0, 0])
    for a, sit in indice:
        nome = a.executores[0] if a.executores else "(sem executor)"
        execs[nome][0] += 1
        execs[nome][1] += sit == "ATRASADO"
    L += ["", "<b>Por executor</b>"]
    for nome, (t, at) in sorted(execs.items(), key=lambda x: (-pct(x[1][1], x[1][0]), -x[1][0])):
        L.append(f"{cor_pct(pct(at, t))} {esc(nome)}: {at}/{t} ({pct(at, t):.0f}%)")

    # por dia
    dias = defaultdict(lambda: [0, 0])
    for a, sit in indice:
        d = (_dt(a.data) or agora).date()
        dias[d][0] += 1
        dias[d][1] += sit == "ATRASADO"
    nomes_dia = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

    # principais apontamentos
    ap = []
    piores = [(p, v) for p, v in planos.items() if v[0] >= 5]
    if piores:
        p, v = max(piores, key=lambda x: pct(x[1][1], x[1][0]))
        if v[1]:
            ap.append(f"Plano com mais atraso: <b>{esc(legivel(p))}</b> ({pct(v[1], v[0]):.0f}% das baixas)")
    pe = [(n, v) for n, v in execs.items() if v[0] >= 5]
    if pe:
        n, v = max(pe, key=lambda x: pct(x[1][1], x[1][0]))
        if v[1]:
            ap.append(f"Executor com mais atraso: <b>{esc(n)}</b> ({v[1]} de {v[0]})")
    if dias:
        d, v = max(dias.items(), key=lambda x: x[1][1])
        if v[1]:
            ap.append(f"Dia com mais atrasos: <b>{nomes_dia[d.weekday()]} {d.strftime('%d/%m')}</b> ({v[1]})")
    if nf_indice:
        cont = defaultdict(int)
        for a in nf_indice:
            cont[legivel(a.descricao)] += 1
        top = sorted(cont.items(), key=lambda x: -x[1])[:3]
        ap.append("Mais vezes sem baixa: " + "; ".join(f"{esc(d)} ({n}x)" for d, n in top))
    oc = oc_dados()["itens"].values()
    novas = [o for o in oc if inicio.isoformat() <= o["criada"][:10] <= fim.isoformat()]
    resolv = [o for o in oc if o.get("resolvida") and inicio.isoformat() <= o["resolvida"]["em"][:10] <= fim.isoformat()]
    abertas_oc = [o for o in oc if not o.get("resolvida")]
    if novas or abertas_oc:
        velha = min(abertas_oc, key=lambda o: o["criada"], default=None)
        txt = f"Ocorrências: {len(novas)} novas, {len(resolv)} resolvidas, {len(abertas_oc)} em aberto"
        if velha:
            txt += f" (mais antiga: #{velha['num']}, de {velha['criada'][8:10]}/{velha['criada'][5:7]})"
        ap.append(txt)
    if ap:
        L += ["", "<b>📌 Principais apontamentos</b>"] + [f"• {x}" for x in ap]
    if not tem_hora_baixa:
        L += ["", "<i>Obs.: o Leankeep não informou o horário das baixas; atrasos contados "
                  "pelo acompanhamento do robô (a partir da instalação).</i>"]
    return "\n".join(L)


def semana_anterior() -> tuple[date, date]:
    h = hoje()
    seg = h - timedelta(days=h.weekday() + 7)
    return seg, seg + timedelta(days=6)


async def enviar_semanal(context: ContextTypes.DEFAULT_TYPE):
    if MODO != "leankeep":
        return
    ini, fim = semana_anterior()
    try:
        txt = await montar_semanal(ini, fim)
    except Exception as e:
        log.exception("Erro no resumo semanal")
        txt = f"⚠️ Não consegui montar o resumo semanal: {esc(e)}"
    await enviar(context.bot, CFG.get("gestor_chat_id"), txt)


async def cmd_semana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MODO != "leankeep":
        await update.message.reply_text("O resumo semanal precisa do acesso ao Leankeep.")
        return
    if context.args and normalizar(context.args[0]).startswith("ATUAL"):
        h = hoje()
        ini, fim = h - timedelta(days=h.weekday()), h
    else:
        ini, fim = semana_anterior()
    await update.message.reply_text(
        f"Montando o resumo de {ini.strftime('%d/%m')} a {fim.strftime('%d/%m')}… pode levar 1–2 min.")
    try:
        txt = await montar_semanal(ini, fim)
    except Exception as e:
        log.exception("Erro no resumo semanal")
        txt = f"⚠️ Não consegui montar o resumo: {esc(e)}"
    await enviar(context.bot, update.effective_chat.id, txt)


# =====================================================================
#  LEMBRETE DIÁRIO NO GRUPO DE FECHAMENTO: conferir data/hora dos relógios
# =====================================================================
def rel_cfg() -> dict:
    return CFG.get("lembrete_relogio", {}) or {}


TEXTO_RELOGIO = (
    "🚨🚨 <b>LEMBRETE DO FECHAMENTO — CONFIRA OS RELÓGIOS!</b> 🚨🚨\n\n"
    "Antes de registrar o <b>fechamento do registro de gás</b> e o "
    "<b>desligamento da exaustão e insuflamento</b>, confira no relógio:\n"
    "   ▫️ <b>Dia e mês</b> corretos\n"
    "   ▫️ <b>Ano</b> correto ({ano})\n"
    "   ▫️ <b>Hora</b> certa\n\n"
    "Se estiver errado, <b>ajuste o relógio antes de tirar a foto</b> e avise a manutenção."
)


async def lembrete_relogio(context: ContextTypes.DEFAULT_TYPE):
    gid = CFG.get("fech_grupo_chat_id")
    if not gid:
        return
    texto = rel_cfg().get("texto") or TEXTO_RELOGIO
    await enviar(context.bot, gid, texto.replace("{ano}", str(hoje().year)),
                 message_thread_id=CFG.get("fech_topico_id") or None)


async def cmd_testar_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if CFG.get("gestor_chat_id") and update.effective_user.id != int(CFG["gestor_chat_id"]):
        await update.message.reply_text("Só o gestor pode testar o lembrete.")
        return
    if not CFG.get("fech_grupo_chat_id"):
        await update.message.reply_text(
            "O grupo de fechamento ainda não foi configurado. Envie /configurar_fechamento dentro dele.")
        return
    await lembrete_relogio(context)
    await update.message.reply_text("✅ Lembrete enviado agora no grupo de fechamento.")


async def cmd_configurar_fechamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if update.effective_chat.type == "private":
        await msg.reply_text("Envie este comando DENTRO do grupo de fechamento.")
        return
    if CFG.get("gestor_chat_id") and update.effective_user.id != int(CFG["gestor_chat_id"]):
        await msg.reply_text("Só o gestor pode configurar o grupo de fechamento.")
        return
    topico = msg.message_thread_id if msg.is_topic_message else 0
    salvar_ajuste(fech_grupo_chat_id=update.effective_chat.id, fech_topico_id=topico)
    await msg.reply_text(
        f"✅ Pronto! Todo dia às {rel_cfg().get('horario', '23:00')} vou lembrar aqui de conferir "
        f"a data e a hora dos relógios do gás e da exaustão.")


# =====================================================================
#  TAGME: alerta de fila de espera e resumo de reservas (tópico "Bot da Tagme")
# =====================================================================
ARQ_FILA = DADOS / "tagme_fila.json"
_TG_ULTIMO_ERRO = [0.0]


def tg_cfg() -> dict:
    return CFG.get("tagme", {}) or {}


def _c(o: dict, *nomes, padrao=None):
    for n in nomes:
        if n in o and o[n] not in (None, ""):
            return o[n]
    return padrao


def _pessoas(o: dict) -> int:
    v = _c(o, "partySize", "size", "quantity", "people", "guests", "pax", padrao=0)
    if isinstance(v, dict):
        v = sum(x for x in v.values() if isinstance(x, (int, float)))
    try:
        return int(v)
    except Exception:
        return 0


def _status(o: dict) -> str:
    v = _c(o, "status", "state", padrao="")
    if isinstance(v, dict):
        v = _c(v, "name", "value", "status", padrao="")
    return str(v).lower()


def _id(o: dict) -> str:
    return str(_c(o, "id", "_id", "entryId", "uuid", "reservationId", padrao=json.dumps(o, sort_keys=True)[:80]))


def _primeiro_nome(o: dict) -> str:
    v = _c(o, "name", "guestName", "customerName", "firstName", padrao="")
    if isinstance(v, dict):
        v = _c(v, "first", "firstName", "name", padrao="")
    if not v:
        for k in ("guest", "customer", "user", "client"):
            if isinstance(o.get(k), dict):
                v = _c(o[k], "name", "firstName", "fullName", padrao="")
                if v:
                    break
    return str(v).split(" ")[0].title() if v else ""


def _hora_local(o: dict):
    """Horário da reserva (hora, minuto)."""
    import re
    for k in ("time", "reservationTime", "hour", "startTime", "date", "reservationDate", "dateTime", "start"):
        v = o.get(k)
        if not isinstance(v, str):
            continue
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", v.strip()):
            h, m = v.strip().split(":")[:2]
            return int(h), int(m)
        d = _dt(v)
        if d and "T" in v:
            if v.endswith("Z") or "+" in v[10:]:
                d = d.astimezone(FUSO)
            if (d.hour, d.minute) != (0, 0):
                return d.hour, d.minute
    return None


def _periodo(o: dict) -> str:
    hm_ = _hora_local(o)
    if not hm_:
        return "Sem horário"
    p = tg_cfg().get("periodos", {}) or {}
    happy, jantar = hm(p.get("happy_hour_inicio", "17:00")), hm(p.get("jantar_inicio", "17:00"))
    if hm_ < happy:
        return "Almoço"
    if hm_ < jantar:
        return "Happy hour"
    return "Jantar"


def _cat_reserva(st: str) -> str:
    if "cancel" in st:
        return "canceladas"
    if "no_show" in st or "noshow" in st or "no-show" in st or "absent" in st:
        return "no-show"
    if any(x in st for x in ("seat", "check", "arriv", "finish", "complet", "attend", "present")):
        return "compareceram"
    return "confirmadas"


def tg_destino():
    return CFG.get("tagme_grupo_chat_id"), CFG.get("tagme_topico_id") or None


async def tg_enviar(bot, texto, **kw):
    gid, top = tg_destino()
    if not gid:
        return await enviar(bot, CFG.get("gestor_chat_id"), texto, **kw)
    return await enviar(bot, gid, texto, message_thread_id=top, **kw)


async def tg_erro(bot, e):
    log.warning("Tagme: %s", e)
    import time as _t
    if _t.time() - _TG_ULTIMO_ERRO[0] > 3600:  # no máximo 1 aviso por hora
        _TG_ULTIMO_ERRO[0] = _t.time()
        await enviar(bot, CFG.get("gestor_chat_id"), f"⚠️ Robô não conseguiu consultar a Tagme: {esc(e)}")


def fila_ativa(itens):
    ativos = [normalizar(x) for x in tg_cfg().get("status_na_fila", ["waiting", "notified"])]
    return [o for o in itens if normalizar(_status(o)) in ativos]


async def vigiar_fila(context: ContextTypes.DEFAULT_TYPE):
    if not TG or not tg_cfg().get("ativo", True):
        return
    try:
        itens = await asyncio.wait_for(asyncio.to_thread(TG.fila), 90)
    except Exception as e:
        await tg_erro(context.bot, e)
        return
    na_fila = fila_ativa(itens)
    estado = ler(ARQ_FILA, {})
    d = hoje().isoformat()
    if estado.get("dia") != d:
        estado = {"dia": d, "vistos": [], "grupos_dia": 0, "pessoas_dia": 0, "pico": 0,
                  "tinha_fila": False, "ontem": estado.get("resumo_dia")}
    vistos = set(estado.get("vistos", []))
    grupos, pessoas = len(na_fila), sum(_pessoas(o) for o in na_fila)
    novos = [o for o in na_fila if _id(o) not in vistos]
    for o in sorted(novos, key=lambda x: _c(x, "position", padrao=999) or 999):
        vistos.add(_id(o))
        estado["grupos_dia"] += 1
        estado["pessoas_dia"] += _pessoas(o)
        await tg_enviar(context.bot, texto_novo_grupo(_pessoas(o), _primeiro_nome(o),
                                                      _c(o, "position"), grupos, pessoas))
    if grupos == 0 and estado.get("tinha_fila") and tg_cfg().get("avisar_fila_zerada", True):
        await tg_enviar(context.bot, "✅ <b>Fila de espera zerada.</b>")
    estado["tinha_fila"] = grupos > 0
    estado["pico"] = max(estado.get("pico", 0), grupos)
    estado["vistos"] = list(vistos)[-500:]
    estado["resumo_dia"] = {"dia": d, "grupos": estado["grupos_dia"], "pessoas": estado["pessoas_dia"],
                            "pico": estado["pico"]}
    gravar(ARQ_FILA, estado)


def texto_novo_grupo(qtd, nome, pos, grupos, pessoas) -> str:
    txt = [f"🕒 <b>Novo grupo na fila de espera</b>",
           f"• Grupo: <b>{plural(qtd, 'pessoa')}</b>" + (f" – {esc(nome)}" if nome else "")
           + (f" (posição {pos})" if pos not in (None, "") else ""),
           f"• Na fila agora: <b>{plural(grupos, 'grupo')}</b>, <b>{plural(pessoas, 'pessoa')}</b>"]
    return "\n".join(txt)


def plural(n: int, palavra: str) -> str:
    return f"{n} {palavra}" + ("" if n == 1 else "s")


_CHAVES_NOME = ("name", "fullname", "full_name", "nome", "nomecompleto", "guestname", "customername",
                "clientname", "contactname", "bookername", "displayname")
_DONO = ("customer", "guest", "client", "contact", "booker", "person", "holder", "owner", "reservationholder",
         "cliente", "consumer", "profile", "user")
_NAO_E_PESSOA = ("venue", "restaurant", "table", "area", "section", "company", "partner", "source", "origin",
                 "channel", "createdby", "updatedby", "employee", "staff", "tag", "occasion", "status", "event")


def _nome_em(d: dict) -> str:
    for k, v in d.items():
        if isinstance(v, str) and v.strip() and k.lower().replace("_", "") in _CHAVES_NOME:
            return v
    ini = next((v for k, v in d.items() if isinstance(v, str) and k.lower().replace("_", "") in ("firstname", "first", "primeironome")), "")
    fim = next((v for k, v in d.items() if isinstance(v, str) and k.lower().replace("_", "") in ("lastname", "last", "surname", "sobrenome")), "")
    return f"{ini} {fim}".strip()


def _procurar_nome(o, prof=0) -> str:
    """Procura o nome do cliente em qualquer lugar da reserva (a Tagme muda o formato às vezes)."""
    if not isinstance(o, dict) or prof > 3:
        return ""
    if prof == 0:
        n = _nome_em(o)
        if n:
            return n
    # primeiro, objetos com cara de "cliente"
    for k, v in o.items():
        kl = k.lower().replace("_", "")
        if isinstance(v, dict) and any(t in kl for t in _DONO) and not any(t in kl for t in _NAO_E_PESSOA):
            n = _nome_em(v) or _procurar_nome(v, prof + 1)
            if n:
                return n
        if isinstance(v, list) and v and isinstance(v[0], dict) and any(t in kl for t in _DONO):
            n = _nome_em(v[0]) or _procurar_nome(v[0], prof + 1)
            if n:
                return n
    # depois, qualquer outro objeto que não seja de restaurante/mesa/etc.
    for k, v in o.items():
        kl = k.lower().replace("_", "")
        if isinstance(v, dict) and not any(t in kl for t in _NAO_E_PESSOA) and not any(t in kl for t in _DONO):
            n = _nome_em(v) if prof < 2 else ""
            if n:
                return n
    return ""


def _nome_completo(o: dict) -> str:
    v = _c(o, "name", "guestName", "customerName", "fullName", padrao="")
    if isinstance(v, dict):
        v = " ".join(str(x) for x in (v.get("first") or v.get("firstName"), v.get("last") or v.get("lastName")) if x)
    if not v:
        for k in ("guest", "customer", "user", "client"):
            if isinstance(o.get(k), dict):
                g = o[k]
                v = _c(g, "name", "fullName", padrao="") or " ".join(
                    str(x) for x in (g.get("firstName"), g.get("lastName")) if x)
                if v:
                    break
    if not v:
        v = _procurar_nome(o)
    v = " ".join(str(v).split()).strip(" -–—.,") if v else ""
    return v.title() if v else "(sem nome)"


def _etiqueta(st: str, realizado: bool) -> str:
    cat = _cat_reserva(st)
    if realizado:
        return {"compareceram": "veio", "no-show": "NÃO VEIO", "canceladas": "cancelada"}.get(cat, "sem check-in")
    if cat == "canceladas":
        return "cancelada"
    if any(x in st for x in ("pend", "unconfirm", "not_confirm", "await", "wait", "request", "pre")):
        return "não confirmada"
    return ""


def texto_reservas(reservas, dia: date, titulo: str, realizado: bool, com_titulo: bool = True) -> list[str]:
    """Bloco de um dia: total, e por período (Almoço / Jantar) cada reserva com hora, nome e pessoas."""
    ativas = [o for o in reservas if _cat_reserva(_status(o)) != "canceladas"]
    canceladas = [o for o in reservas if _cat_reserva(_status(o)) == "canceladas"]
    L = [f"<b>Reservas {titulo_dia(titulo)} ({dia.strftime('%d/%m')})</b>"] if com_titulo else []
    if not ativas:
        L += ["Nenhuma reserva."]
        if canceladas:
            L.append(f"Canceladas: {len(canceladas)}")
        return L
    L.append(f"Total: <b>{plural(len(ativas), 'reserva')}</b> · <b>{plural(sum(_pessoas(o) for o in ativas), 'pessoa')}</b>")
    por = defaultdict(list)
    for o in ativas:
        por[_periodo(o)].append(o)
    icone = {"Almoço": "☀️", "Happy hour": "🍹", "Jantar": "🌙", "Sem horário": ""}
    for per in ["Almoço", "Happy hour", "Jantar", "Sem horário"]:
        lista = por.get(per)
        if not lista:
            continue
        lista.sort(key=lambda o: (_hora_local(o) or (99, 0), -_pessoas(o)))
        # emoji depois da palavra: no Telegram do Pedro, linha que começa com emoji perde a quebra
        L += ["", f"<b>{per} {icone[per]}</b> – {plural(len(lista), 'reserva')} · "
                  f"{plural(sum(_pessoas(o) for o in lista), 'pessoa')}"]
        for o in lista[:60]:
            h = _hora_local(o)
            hora = f"{h[0]:02d}:{h[1]:02d}" if h else "--:--"
            et = _etiqueta(_status(o), realizado)
            L.append(f"{hora} – {esc(_nome_completo(o))} – {plural(_pessoas(o), 'pessoa')}"
                     + (f" <i>({et})</i>" if et else ""))
        if len(lista) > 60:
            L.append(f"… e mais {len(lista) - 60}")
    if canceladas:
        L += ["", f"Canceladas: {len(canceladas)} ({plural(sum(_pessoas(o) for o in canceladas), 'pessoa')})"]
    return L


def titulo_dia(titulo: str) -> str:
    return {"Amanhã": "de amanhã", "Hoje": "de hoje", "Ontem": "de ontem"}.get(titulo, "do dia")


def botoes_tagme() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Reservas de amanhã", callback_data="tg|amanha"),
                                  InlineKeyboardButton("🕒 Hoje e fila agora", callback_data="tg|agora")]])


def texto_fila(itens) -> list[str]:
    na = fila_ativa(itens)
    if not na:
        return ["", "<b>Fila de espera agora</b>", "• Ninguém na fila."]
    L = ["", f"<b>Fila de espera agora: {plural(len(na), 'grupo')}, "
             f"{plural(sum(_pessoas(o) for o in na), 'pessoa')}</b>"]
    for o in sorted(na, key=lambda x: _c(x, "position", padrao=999) or 999):
        nome = _primeiro_nome(o)
        L.append(f"• {plural(_pessoas(o), 'pessoa')}" + (f" – {esc(nome)}" if nome else "")
                 + (" (já chamado)" if "notif" in _status(o) else ""))
    return L


CABECALHO_TG = ["📅 <b>Reservas – Coco Bambu Jundiaí</b>", ""]


async def montar_tagme(modo: str) -> str:
    """modo: 'resumo' (ontem + hoje), 'agora' (hoje + fila), 'amanha', ou uma data ISO."""
    h = hoje()
    agora_txt = datetime.now(FUSO).strftime("%d/%m %H:%M")
    L = list(CABECALHO_TG)
    if modo == "resumo":
        ontem = h - timedelta(days=1)
        r_ontem = await asyncio.wait_for(asyncio.to_thread(TG.reservas, ontem), 120)
        r_hoje = await asyncio.wait_for(asyncio.to_thread(TG.reservas, h), 120)
        L += texto_reservas(r_ontem, ontem, "Ontem", realizado=True)
        est = ler(ARQ_FILA, {})
        f = est.get("ontem") if est.get("dia") == h.isoformat() else est.get("resumo_dia")
        if f and f.get("dia") == ontem.isoformat():
            L += ["", f"Fila de espera: {plural(f['grupos'], 'grupo')}, {plural(f['pessoas'], 'pessoa')} (pico de {f['pico']})"]
        L += [""] + texto_reservas(r_hoje, h, "Hoje", realizado=False)
    elif modo == "agora":
        r_hoje = await asyncio.wait_for(asyncio.to_thread(TG.reservas, h), 120)
        fila = await asyncio.wait_for(asyncio.to_thread(TG.fila), 90)
        L += texto_reservas(r_hoje, h, "Hoje", realizado=False)
        L += texto_fila(fila)
        est = ler(ARQ_FILA, {})
        if est.get("dia") == h.isoformat() and est.get("grupos_dia"):
            L.append(f"Hoje já passaram pela fila: {plural(est['grupos_dia'], 'grupo')}, "
                     f"{plural(est['pessoas_dia'], 'pessoa')} (pico de {est.get('pico', 0)})")
    else:
        dia = h + timedelta(days=1) if modo == "amanha" else date.fromisoformat(modo)
        r = await asyncio.wait_for(asyncio.to_thread(TG.reservas, dia), 120)
        titulo = "Amanhã" if dia == h + timedelta(days=1) else ("Hoje" if dia == h else
                 ("Ontem" if dia == h - timedelta(days=1) else "Reservas do dia"))
        L = [f"📅 <b>{esc(CFG.get('unidade_nome', 'Coco Bambu Jundiaí'))} – Reservas {titulo_dia(titulo)} "
             f"({dia.strftime('%d/%m')})</b>"]
        L += texto_reservas(r, dia, titulo, realizado=dia < h, com_titulo=False)
    L += ["", f"<i>Atualizado em {agora_txt}</i>"]
    return "\n".join(L)


async def resumo_tagme(context: ContextTypes.DEFAULT_TYPE, chat_destino=None, thread=None, modo=None):
    modo = modo or tg_cfg().get("resumo_modo", "amanha")
    if not TG:
        return
    try:
        texto = await montar_tagme(modo)
    except Exception as e:
        await tg_erro(context.bot, e)
        if chat_destino:
            await enviar(context.bot, chat_destino, f"Erro ao consultar a Tagme: {esc(e)}",
                         message_thread_id=thread)
        return
    if chat_destino:
        await enviar(context.bot, chat_destino, texto, message_thread_id=thread, reply_markup=botoes_tagme())
    else:
        await tg_enviar(context.bot, texto, reply_markup=botoes_tagme())


async def botao_tagme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    modo = q.data.split("|", 1)[1]
    if not TG:
        await q.answer("A Tagme não está configurada.")
        return
    await q.answer("Consultando a Tagme…")
    try:
        texto = await montar_tagme(modo)
    except Exception as e:
        await q.answer(f"Erro: {e}"[:190], show_alert=True)
        return
    try:
        await q.edit_message_text(limpar_texto(texto), parse_mode=ParseMode.HTML, reply_markup=botoes_tagme(),
                                  disable_web_page_preview=True)
    except Exception as e:  # mensagem igual ou longa demais: manda nova
        if "not modified" not in str(e).lower():
            await enviar(context.bot, q.message.chat.id, texto, reply_markup=botoes_tagme(),
                         message_thread_id=q.message.message_thread_id if q.message.is_topic_message else None)


def _onde(update):
    msg = update.message
    return update.effective_chat.id, (msg.message_thread_id if msg.is_topic_message else None)


async def cmd_fila(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not TG:
        await update.message.reply_text("A Tagme ainda não está configurada (TAGME_EMAIL/TAGME_SENHA no .env).")
        return
    chat, top = _onde(update)
    await resumo_tagme(context, chat_destino=chat, thread=top, modo="agora")


async def cmd_reservas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reservas (amanhã) · /reservas hoje|ontem|agora|25/12"""
    if not TG:
        await update.message.reply_text("A Tagme ainda não está configurada (TAGME_EMAIL/TAGME_SENHA no .env).")
        return
    modo = "amanha"
    if context.args:
        a = normalizar(context.args[0]).lower()
        h = hoje()
        if a.startswith("amanh"):
            modo = "amanha"
        elif a.startswith("hoje"):
            modo = h.isoformat()
        elif a.startswith("ontem"):
            modo = (h - timedelta(days=1)).isoformat()
        elif a.startswith("agora"):
            modo = "agora"
        else:
            try:
                d, m = map(int, a.split("/")[:2])
                alvo = date(h.year, m, d)
                if (h - alvo).days > 60:  # ex.: em dezembro, "05/01" é do ano que vem
                    alvo = date(h.year + 1, m, d)
                modo = alvo.isoformat()
            except Exception:
                await update.message.reply_text("Use: /reservas (amanhã) · /reservas hoje · /reservas 25/12")
                return
    await update.message.reply_text("Consultando a Tagme…")
    chat, top = _onde(update)
    await resumo_tagme(context, chat_destino=chat, thread=top, modo=modo)


async def cmd_configurar_tagme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if update.effective_chat.type == "private":
        await msg.reply_text("Envie este comando DENTRO do tópico \"Bot da Tagme\" do grupo.")
        return
    if CFG.get("gestor_chat_id") and update.effective_user.id != int(CFG["gestor_chat_id"]):
        await msg.reply_text("Só o gestor pode configurar.")
        return
    topico = msg.message_thread_id if msg.is_topic_message else 0
    salvar_ajuste(tagme_grupo_chat_id=update.effective_chat.id, tagme_topico_id=topico)
    await msg.reply_text("✅ Pronto! Alertas de fila de espera e o resumo de reservas da Tagme virão para cá.")


# =====================================================================
#  /reenviar — menu para mandar de novo os envios automáticos
# =====================================================================
def _reenvios():
    """chave -> (rótulo do botão, função, destino em texto)"""
    r = {
        "lista": ("📋 Ocorrências em aberto (fotos do grupo)", oc_publicar_lista, "grupo da manutenção + seu privado"),
        "resumo": ("📊 Leankeep: o que ficou sem baixa ONTEM", resumo, "seu privado"),
        "reservas": ("📅 Reservas da Tagme (amanhã)", resumo_tagme, "tópico Bot da Tagme"),
        "relogio": ("⏰ Lembrete dos relógios", lembrete_relogio, "grupo de fechamento"),
        "semanal": ("📈 Resumo semanal", enviar_semanal, "seu privado"),
    }
    for i, cob in enumerate(CFG.get("cobrancas", [])):
        r[f"cob{i}"] = (f"🔔 Cobrança: {cob['nome']}", None, "tópico Leankeep diário")
    for i, lem in enumerate(CFG.get("lembretes", [])):
        r[f"lem{i}"] = (f"🚨 Lembrete: {lem.get('texto', '')[:40]}", None, f"onde: {lem.get('onde', 'leankeep')}")
    return r


def _eh_gestor(update) -> bool:
    g = CFG.get("gestor_chat_id")
    return not g or update.effective_user.id == int(g)


async def cmd_reenviar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _eh_gestor(update):
        await update.message.reply_text("Só o gestor pode reenviar.")
        return
    opcoes = _reenvios()
    if context.args:  # atalho: /reenviar lista
        chave = normalizar(context.args[0]).lower()
        if chave in opcoes:
            await executar_reenvio(context, chave, update.message.reply_text)
            return
    botoes = [[InlineKeyboardButton(rot, callback_data=f"re|{k}")] for k, (rot, _, _) in opcoes.items()]
    await update.message.reply_text("O que você quer reenviar?", reply_markup=InlineKeyboardMarkup(botoes))


async def executar_reenvio(context, chave, responder):
    rot, func, destino = _reenvios()[chave]
    try:
        if chave.startswith("lem"):
            lem = CFG["lembretes"][int(chave[3:])]
            texto, _ = await montar_lembrete(lem)
            if not texto:
                await responder(f"✅ Tudo baixado no Leankeep: {lem.get('texto', '')}. Não enviei o lembrete.")
                return
            gid, top = destino_lembrete(lem.get("onde"))
            await enviar(context.bot, gid, texto, message_thread_id=top)
        elif chave.startswith("cob"):
            cob = CFG["cobrancas"][int(chave[3:])]
            if MODO == "leankeep":
                pend = await buscar_pendentes(cob.get("palavras"), data_ref(cob), cob.get("excluir"))
                if not pend:
                    await responder(f"✅ Nada pendente no Leankeep para: {cob['nome']}. Não enviei cobrança.")
                    return
                await cobrar_leankeep(context, cob, 0)
            else:
                await cobrar_manual(context, cob, 0)
        else:
            await func(context)
        await responder(f"✅ Reenviado: {rot.split(' ', 1)[1]}\nDestino: {destino}")
    except Exception as e:
        log.exception("Erro no reenvio")
        await responder(f"⚠️ Não consegui reenviar: {e}")


async def botao_reenviar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    g = CFG.get("gestor_chat_id")
    if g and q.from_user.id != int(g):
        await q.answer("Só o gestor pode reenviar.", show_alert=True)
        return
    chave = q.data.split("|", 1)[1]
    if chave not in _reenvios():
        await q.answer("Opção não existe mais.")
        return
    await q.answer("Enviando…")

    async def responder(txt):
        try:
            await q.edit_message_text(txt)
        except Exception:
            await enviar(context.bot, q.message.chat.id, esc(txt))
    await executar_reenvio(context, chave, responder)


# =====================================================================
#  /testar: mostra no seu privado como fica cada mensagem do robô
# =====================================================================
COMANDOS_TXT = (
    "📖 <b>Comandos do robô</b>\n\n"
    "<b>No dia a dia</b>\n"
    "• /pendentes – o que falta baixar agora no Leankeep\n"
    "• /pendencias – lista de ocorrências em aberto\n"
    "• /resolver N – conclui a ocorrência N\n"
    "• /reativar N – reabre uma ocorrência baixada por engano\n"
    "• /excluir N – apaga a ocorrência N\n"
    "• /semana – resumo da semana passada (/semana atual = semana corrente)\n"
    "• /fila – fila de espera da Tagme agora\n"
    "• /reservas – reservas de amanhã (/reservas hoje, /reservas 25/12)\n"
    "• /alerta – manda um alerta 🚨 para o tópico que você escolher\n"
    "• /reenviar – manda de novo algo que não chegou\n"
    "• /testar – este menu de teste\n\n"
    "<b>Consulta</b>\n"
    "• /agenda – horários das cobranças\n"
    "• /lembretes – lembretes cadastrados\n\n"
    "<b>Configuração (só uma vez)</b>\n"
    "• /sou_gestor – no privado\n"
    "• /configurar_topico – dentro do tópico Leankeep diário\n"
    "• /configurar_ocorrencias – dentro do tópico onde a equipe posta as fotos\n"
    "• /configurar_keepbot – dentro do tópico Keepbot (lista e avisos)\n"
    "• /configurar_fechamento – dentro do grupo de fechamento\n"
    "• /configurar_tagme – dentro do tópico Bot da Tagme\n"
    "• /cadastrar Nome – colaborador se identifica (nome igual ao Leankeep)"
)


def exemplo_ativs(cob=None):
    plano = (cob or {}).get("nome", "Exemplo")
    return [Atividade(numero=f"EX{i}", descricao=d, unidade=CFG.get("unidade", ""), plano=plano,
                      data=hoje().isoformat(), status="1", executores=["Colaborador Exemplo"])
            for i, d in enumerate(["Atividade de exemplo 1 (fictícia)",
                                   "Atividade de exemplo 2 (fictícia)"], 1)]


def _testes():
    """chave -> rótulo. A ordem aqui é a ordem do menu."""
    t = {"h_lk": "— LEANKEEP —"}
    for i, cob in enumerate(CFG.get("cobrancas", [])):
        t[f"c{i}a"] = f"🔔 {cob['nome']} – aviso"
        t[f"c{i}v"] = f"🚨 {cob['nome']} – vencido"
    t.update({
        "conf_ok": "✅ Resposta ao \"baixei\" (tudo certo)",
        "conf_falta": "⚠️ Pendência após o \"baixei\" (só no seu privado)",
        "atraso": "⚠️ Alerta de baixa em atraso",
        "resumo": "📊 Resumo diário (o que ficou de ontem)",
        "semanal": "📈 Resumo semanal",
        "h_oc": "— OCORRÊNCIAS —",
        "oc_reg": "📌 Confirmação de nova ocorrência",
        "oc_lista": "📋 Lista de ocorrências (7h)",
        "h_fe": "— FECHAMENTO —",
        "relogio": "⏰ Lembrete dos relógios (23h)",
        "h_tg": "— TAGME —",
        "fila": "🕒 Alerta de novo grupo na fila",
        "res_amanha": "📅 Reservas de amanhã",
        "res_agora": "🕒 Reservas de hoje + fila agora",
    })
    if CFG.get("lembretes"):
        t["h_lem"] = "— LEMBRETES —"
        for i, lem in enumerate(CFG["lembretes"]):
            t[f"l{i}"] = f"🚨 {lem.get('horario', '')} {lem.get('texto', '')[:32]}"
    t["h_fim"] = "— OUTROS —"
    t["comandos"] = "📖 Lista de comandos"
    t["tudo"] = "▶️ Mandar tudo de uma vez"
    return t


def menu_testes() -> InlineKeyboardMarkup:
    linhas, par = [], []
    for k, rot in _testes().items():
        if k.startswith("h_"):
            if par:
                linhas.append(par)
                par = []
            linhas.append([InlineKeyboardButton(rot, callback_data="tx|titulo")])
        elif k[0] == "c" and k[-1] in "av" and k[1:-1].isdigit():
            par.append(InlineKeyboardButton(rot, callback_data=f"tt|{k}"))
            if len(par) == 2:
                linhas.append(par)
                par = []
        else:
            if par:
                linhas.append(par)
                par = []
            linhas.append([InlineKeyboardButton(rot, callback_data=f"tt|{k}")])
    if par:
        linhas.append(par)
    return InlineKeyboardMarkup(linhas)


async def cmd_testar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _eh_gestor(update):
        await update.message.reply_text("Só o gestor pode usar o modo de teste.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /testar no meu privado, para o teste não aparecer no grupo.")
        return
    await update.message.reply_text(
        "🧪 <b>Modo de teste</b>\n\n"
        "Toque numa opção e eu mando AQUI como a mensagem fica.\n"
        "Nada vai para os grupos e nada é alterado.\n\n"
        "<i>Leankeep e Tagme usam os dados reais de agora. Quando não há nada pendente, "
        "uso atividades de exemplo. Os botões das mensagens de teste são só demonstração.</i>",
        parse_mode=ParseMode.HTML, reply_markup=menu_testes())


async def executar_teste(context, chave, chat_id):
    """Roda numa tarefa própria: o desvio para o privado vale só aqui dentro."""
    TESTE.set(chat_id)
    EXEMPLO.set(None)
    bot = context.bot
    aviso = None
    if chave[0] == "c" and chave[-1] in "av" and chave[1:-1].isdigit():
        cob = CFG["cobrancas"][int(chave[1:-1])]
        pend = []
        if MODO == "leankeep":
            try:
                pend = await buscar_pendentes(cob.get("palavras"), data_ref(cob), cob.get("excluir"))
            except Exception as e:
                aviso = f"Leankeep não respondeu ({e}); usei exemplo."
        if not pend:
            EXEMPLO.set(exemplo_ativs(cob))
            aviso = aviso or "Nada pendente agora no Leankeep, então usei atividades de exemplo."
        if MODO == "leankeep":
            await cobrar_leankeep(context, cob, 0 if chave.endswith("a") else 2)
        else:
            EXEMPLO.set(None)
            await cobrar_manual(context, cob, 0 if chave.endswith("a") else 2)
    elif chave == "conf_ok":
        cobs = CFG.get("cobrancas", [])[:1]
        await enviar(bot, CFG.get("grupo_chat_id"), esc(texto_tudo_baixado(cobs, "Fulano")))
    elif chave == "conf_falta":
        cob = CFG["cobrancas"][0]
        await enviar(bot, CFG.get("gestor_chat_id"),
                     texto_pendencia_privado("Fulano", datetime.now(FUSO).strftime("%H:%M"),
                                             [(cob, data_ref(cob), exemplo_ativs(cob))]))
    elif chave == "atraso":
        cob = next((c for c in CFG.get("cobrancas", []) if c.get("avisar_baixa_atrasada", True)),
                   CFG["cobrancas"][0])
        agora = datetime.now(FUSO)
        janela = f"entre {(agora - timedelta(minutes=10)).strftime('%H:%M')} e {agora.strftime('%H:%M')}"
        await enviar(bot, CFG.get("gestor_chat_id"),
                     texto_baixa_atrasada(cob, hoje(), janela, [a.descricao for a in exemplo_ativs(cob)]))
    elif chave == "resumo":
        await resumo(context)
    elif chave == "semanal":
        await bot.send_message(chat_id, "Montando o resumo semanal… pode levar 1–2 min.")
        await enviar_semanal(context)
    elif chave == "oc_reg":
        sc, st = oc_saida()
        if saida_separada():
            txt = ("📌 <b>Ocorrência #12 registrada</b> – 🔴 Pendente\n\n"
                   "Lâmpada queimada no corredor da cozinha (exemplo)\n"
                   f"<i>Fulano · {datetime.now(FUSO).strftime('%d/%m %H:%M')}</i> · 📷 ver foto\n\n"
                   "Use os botões abaixo para atualizar.")
        else:
            txt = ("📌 Ocorrência #12 registrada – 🔴 Pendente\n\n"
                   "Use os botões abaixo (ou responda a foto com \"em andamento\" ou \"resolvido\").")
        await enviar(bot, sc, txt, message_thread_id=st or None, reply_markup=botao_oc("12"))
    elif chave == "oc_lista":
        texto, teclado = oc_texto_lista()
        extra = {"reply_markup": teclado} if teclado else {}
        sc, st = oc_saida()
        await enviar(bot, sc, texto, message_thread_id=st or None, **extra)
    elif chave == "relogio":
        texto = rel_cfg().get("texto") or TEXTO_RELOGIO
        await enviar(bot, CFG.get("fech_grupo_chat_id"), texto.replace("{ano}", str(hoje().year)))
    elif chave == "fila":
        await tg_enviar(bot, texto_novo_grupo(4, "Maria", 3, 3, 11))
        aviso = "Grupo e nomes deste alerta são de exemplo."
    elif chave in ("res_amanha", "res_agora"):
        if not TG:
            await bot.send_message(chat_id, "A Tagme não está configurada no .env.")
        else:
            texto = await montar_tagme("amanha" if chave == "res_amanha" else "agora")
            await tg_enviar(bot, texto, reply_markup=botoes_tagme())
    elif chave.startswith("l") and chave[1:].isdigit():
        lem = CFG["lembretes"][int(chave[1:])]
        texto, cob = await montar_lembrete(lem)
        if not texto:
            texto, _ = await montar_lembrete(lem, exemplo=True)
            aviso = "Agora está tudo baixado no Leankeep, então no dia a dia este lembrete NÃO sairia. Mostrei com atividades de exemplo."
        gid, top = destino_lembrete(lem.get("onde"))
        await enviar(bot, gid, texto, message_thread_id=top)
    elif chave == "comandos":
        await bot.send_message(chat_id, COMANDOS_TXT, parse_mode=ParseMode.HTML)
    if aviso:
        await bot.send_message(chat_id, f"ℹ️ {aviso}")


async def _rodar_teste(context, chave, chat_id):
    try:
        if chave == "tudo":
            for k in _testes():
                if k.startswith("h_") or k in ("tudo", "comandos", "semanal") or k.endswith("v"):
                    continue
                try:
                    await executar_teste(context, k, chat_id)
                except Exception as e:
                    log.exception("Erro no teste %s", k)
                    await context.bot.send_message(chat_id, f"⚠️ Erro no teste \"{_testes()[k]}\": {e}")
                await asyncio.sleep(1)
            await context.bot.send_message(
                chat_id, "✅ Pronto! Para ver o resumo semanal ou a cobrança de prazo vencido, "
                         "toque nos botões próprios. /testar abre o menu de novo.")
        else:
            await executar_teste(context, chave, chat_id)
    except Exception as e:
        log.exception("Erro no teste")
        await context.bot.send_message(chat_id, f"⚠️ Erro no teste: {e}")


async def botao_teste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.data.startswith("tx|"):
        if q.data == "tx|titulo":
            await q.answer()
        else:
            await q.answer("Botão de demonstração. Na mensagem de verdade, no grupo, ele funciona.",
                           show_alert=True)
        return
    g = CFG.get("gestor_chat_id")
    if g and q.from_user.id != int(g):
        await q.answer("Só o gestor pode testar.", show_alert=True)
        return
    chave = q.data.split("|", 1)[1]
    if chave not in _testes():
        await q.answer("Opção não existe mais. Envie /testar de novo.")
        return
    await q.answer("Gerando o teste…")
    # tarefa separada: o desvio para o privado não vaza para o resto do robô
    asyncio.create_task(_rodar_teste(context, chave, q.message.chat.id))


# =====================================================================
#  /alerta: alertas prontos ou escritos na hora, enviados para o tópico escolhido
# =====================================================================
ARQ_ALERTA = DADOS / "alertas_pendentes.json"


def al_cfg() -> dict:
    return CFG.get("alertas", {}) or {}


def al_destinos() -> dict:
    """chave -> (rótulo do botão, chat, tópico). Só aparecem os destinos configurados."""
    nomes = al_cfg().get("nomes", {}) or {}
    d = {}
    if CFG.get("grupo_chat_id"):
        d["leankeep"] = (nomes.get("leankeep", "📋 Leankeep diário"), CFG["grupo_chat_id"], CFG.get("topico_id") or None)
    if saida_separada():
        c, t = oc_saida()
        d["keepbot"] = (nomes.get("keepbot", "🤖 Keepbot"), c, t or None)
    if CFG.get("oc_grupo_chat_id"):
        d["ocorrencias"] = (nomes.get("ocorrencias", "📸 Batidão semanal"), CFG["oc_grupo_chat_id"],
                            CFG.get("oc_topico_id") or None)
    if CFG.get("fech_grupo_chat_id"):
        d["fechamento"] = (nomes.get("fechamento", "🔒 Grupo do fechamento"), CFG["fech_grupo_chat_id"],
                           CFG.get("fech_topico_id") or None)
    if CFG.get("tagme_grupo_chat_id"):
        d["tagme"] = (nomes.get("tagme", "📅 Bot da Tagme"), CFG["tagme_grupo_chat_id"],
                      CFG.get("tagme_topico_id") or None)
    if CFG.get("gestor_chat_id"):
        d["eu"] = ("👤 Só para mim (teste)", CFG["gestor_chat_id"], None)
    return d


def texto_alerta(texto: str, marcar=None) -> str:
    """1ª linha vira o título com sirenes; o resto, texto normal; no fim, as marcações."""
    linhas = [l.strip() for l in str(texto).strip().splitlines()]
    titulo, resto = linhas[0], "\n".join(linhas[1:]).strip()
    L = [f"🚨🚨 <b>{esc(titulo.upper())}</b> 🚨🚨"]
    if resto:
        L += ["", esc(resto)]
    marcar = al_cfg().get("marcar", []) if marcar is None else marcar
    if marcar:
        L += ["", marcacoes(marcar)]
    return "\n".join(L)


def al_pendente(uid, novo=None, apagar=False):
    dados = ler(ARQ_ALERTA, {})
    limite = (datetime.now(FUSO) - timedelta(hours=3)).isoformat()
    dados = {k: v for k, v in dados.items() if v.get("em", "") >= limite}
    if apagar:
        dados.pop(str(uid), None)
    elif novo is not None:
        novo["em"] = datetime.now(FUSO).isoformat(timespec="seconds")
        dados[str(uid)] = novo
    gravar(ARQ_ALERTA, dados)
    return dados.get(str(uid))


def botoes_destino() -> InlineKeyboardMarkup:
    linhas = [[InlineKeyboardButton(rot, callback_data=f"al|d|{k}")] for k, (rot, _, _) in al_destinos().items()]
    linhas.append([InlineKeyboardButton("❌ Cancelar", callback_data="al|x")])
    return InlineKeyboardMarkup(linhas)


def menu_alertas() -> InlineKeyboardMarkup:
    linhas = []
    for i, a in enumerate(al_cfg().get("prontos", []) or []):
        linhas.append([InlineKeyboardButton(f"🚨 {a.get('texto', '')[:60]}", callback_data=f"al|p|{i}")])
    linhas.append([InlineKeyboardButton("✍️ Escrever outro alerta", callback_data="al|novo")])
    return InlineKeyboardMarkup(linhas)


async def pedir_destino(responder, pend):
    await responder(
        texto_alerta(pend["texto"], pend.get("marcar")) + "\n\n<b>Para onde envio este alerta?</b> Escolha abaixo:",
        botoes_destino())


async def cmd_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _eh_gestor(update):
        await update.message.reply_text("Só o gestor pode enviar alertas.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("Use /alerta no meu privado.")
        return
    uid = update.effective_user.id

    async def responder(txt, teclado=None):
        await enviar(context.bot, update.effective_chat.id, txt, reply_markup=teclado)

    livre = (update.message.text or "").split(maxsplit=1)
    if len(livre) > 1 and livre[1].strip():  # /alerta texto livre
        pend = al_pendente(uid, {"texto": livre[1].strip(), "marcar": None})
        await pedir_destino(responder, pend)
        return
    await enviar(context.bot, update.effective_chat.id,
                 "🚨 <b>Alertas</b>\n\nEscolha um alerta pronto ou escreva o seu.\n"
                 "<i>Dica: também dá para mandar direto: /alerta TEXTO DO ALERTA</i>",
                 reply_markup=menu_alertas())


async def botao_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    g = CFG.get("gestor_chat_id")
    if g and q.from_user.id != int(g):
        await q.answer("Só o gestor pode enviar alertas.", show_alert=True)
        return
    partes = q.data.split("|")
    uid = q.from_user.id
    await q.answer()

    async def responder(txt, teclado=None):
        try:
            await q.edit_message_text(limpar_texto(txt), parse_mode=ParseMode.HTML, reply_markup=teclado,
                                      disable_web_page_preview=True)
        except Exception:
            await enviar(context.bot, q.message.chat.id, txt, reply_markup=teclado)

    if partes[1] == "p":
        prontos = al_cfg().get("prontos", []) or []
        i = int(partes[2])
        if i >= len(prontos):
            await responder("Esse alerta não existe mais. Mande /alerta de novo.")
            return
        a = prontos[i]
        texto = a.get("texto", "") + (("\n" + a["detalhe"]) if a.get("detalhe") else "")
        pend = al_pendente(uid, {"texto": texto, "marcar": a.get("marcar")})
        await pedir_destino(responder, pend)
    elif partes[1] == "novo":
        al_pendente(uid, {"texto": "", "marcar": None, "aguardando": True})
        await responder("✍️ <b>Escreva o alerta e me mande aqui como mensagem.</b>\n\n"
                        "A 1ª linha vira o título com as sirenes. Se quiser, escreva mais detalhes nas linhas de baixo.\n\n"
                        "<i>Para desistir: /cancelar</i>")
    elif partes[1] == "x":
        al_pendente(uid, apagar=True)
        await responder("❌ Alerta cancelado.")
    elif partes[1] == "d":
        pend = al_pendente(uid)
        dest = al_destinos().get(partes[2])
        if not pend or not pend.get("texto") or not dest:
            await responder("Esse alerta expirou. Mande /alerta de novo.")
            return
        rot, chat, top = dest
        m = await enviar(context.bot, chat, texto_alerta(pend["texto"], pend.get("marcar")), message_thread_id=top)
        al_pendente(uid, apagar=True)
        if m is None:
            await responder(f"⚠️ Não consegui enviar para {esc(rot)}. Confira se o robô está nesse grupo.")
        else:
            await responder(texto_alerta(pend["texto"], pend.get("marcar"))
                            + f"\n\n<b>Enviado para {esc(rot)} às {datetime.now(FUSO):%H:%M}.</b>")


async def msg_privada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Texto no privado: se o gestor tocou em 'Escrever outro alerta', vira o alerta."""
    msg = update.effective_message
    if not msg or not msg.text or not _eh_gestor(update):
        return
    pend = al_pendente(update.effective_user.id)
    if not pend or not pend.get("aguardando"):
        return
    pend = al_pendente(update.effective_user.id, {"texto": msg.text.strip(), "marcar": None})

    async def responder(txt, teclado=None):
        await enviar(context.bot, msg.chat.id, txt, reply_markup=teclado)
    await pedir_destino(responder, pend)


async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    al_pendente(update.effective_user.id, apagar=True)
    await update.message.reply_text("❌ Cancelado.")


# =====================================================================
#  LEMBRETES PERSONALIZADOS (texto + horário + pessoas marcadas com @)
# =====================================================================
def destino_lembrete(onde: str):
    onde = normalizar(onde or "leankeep").lower()
    if onde.startswith("keep"):
        c, t = oc_saida()
        return c, t or None
    if onde.startswith("ocorr"):
        return CFG.get("oc_grupo_chat_id"), CFG.get("oc_topico_id") or None
    if onde.startswith("fecha"):
        return CFG.get("fech_grupo_chat_id"), CFG.get("fech_topico_id") or None
    if onde.startswith("tagme"):
        return CFG.get("tagme_grupo_chat_id"), CFG.get("tagme_topico_id") or None
    if onde.startswith("gestor") or onde.startswith("privado"):
        return CFG.get("gestor_chat_id"), None
    return CFG.get("grupo_chat_id"), CFG.get("topico_id") or None


def texto_lembrete(lem: dict, pendentes=None) -> str:
    L = [f"🚨🚨 <b>{esc(lem.get('texto', '').upper())}</b> 🚨🚨"]
    if lem.get("detalhe"):
        L += ["", esc(lem["detalhe"])]
    if pendentes:
        L += ["", f"Ainda falta baixar no Leankeep ({len(pendentes)}):"]
        L += bloco_atividades(pendentes, cadastros(), limite=30)
    if lem.get("marcar"):
        L += ["", marcacoes(lem["marcar"])]
    return "\n".join(L)


def cobranca_do_lembrete(lem: dict):
    nome = normalizar(lem.get("conferir", ""))
    if not nome:
        return None
    return next((c for c in CFG.get("cobrancas", []) if normalizar(c["nome"]) == nome), None)


async def montar_lembrete(lem: dict, exemplo: bool = False):
    """Devolve (texto, cobrança) ou (None, cobrança) quando o Leankeep mostra tudo baixado.
    Se o lembrete tem 'conferir', consulta o Leankeep antes e lista o que falta."""
    cob = cobranca_do_lembrete(lem)
    if not cob or MODO != "leankeep":
        return texto_lembrete(lem), cob
    if exemplo:
        return texto_lembrete(lem, exemplo_ativs(cob)), cob
    try:
        pend = sem_duplicadas(await buscar_pendentes(cob.get("palavras"), data_ref(cob), cob.get("excluir")))
    except Exception as e:  # Leankeep fora do ar: melhor lembrar do que não lembrar
        log.warning("Lembrete '%s': Leankeep não respondeu (%s); enviando sem conferir", lem.get("texto"), e)
        return texto_lembrete(lem), cob
    if not pend:
        log.info("Lembrete '%s' não enviado: tudo baixado no Leankeep", lem.get("texto"))
        return None, cob
    return texto_lembrete(lem, pend), cob


async def enviar_lembrete(context: ContextTypes.DEFAULT_TYPE):
    lem = context.job.data
    texto, cob = await montar_lembrete(lem)
    if not texto:
        return
    gid, top = destino_lembrete(lem.get("onde"))
    extra = {"reply_markup": botoes_baixa(cob)} if cob and MODO == "leankeep" else {}
    m = await enviar(context.bot, gid, texto, message_thread_id=top, **extra)
    if m is not None and cob:  # quem responder "baixei" a este lembrete é conferido nesta cobrança
        MSG_COBRANCA[m.message_id] = CFG["cobrancas"].index(cob)


def agendar_lembretes(jq):
    for i, lem in enumerate(CFG.get("lembretes", [])):
        if not lem.get("ativo", True):
            continue
        dias = [DIAS[d] for d in lem.get("dias", DIAS.keys())]
        jq.run_daily(enviar_lembrete, hora(lem["horario"]), days=tuple(sorted((d + 1) % 7 for d in dias)),
                     data=lem, name=f"lembrete#{i}", job_kwargs={"misfire_grace_time": 900, "coalesce": True})
        log.info("Agendado lembrete: %s às %s", lem.get("texto"), lem["horario"])


async def cmd_lembretes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lst = CFG.get("lembretes", [])
    if not lst:
        await update.message.reply_text("Nenhum lembrete cadastrado (bloco [[lembretes]] no config.toml).")
        return
    L = ["⏰ <b>Lembretes cadastrados</b>"]
    for lem in lst:
        L += ["", f"<b>{esc(lem.get('horario'))}</b> – {esc(lem.get('texto'))}",
              f"Dias: {', '.join(lem.get('dias', DIAS.keys()))} · Onde: {esc(lem.get('onde', 'leankeep'))}",
              f"Marca: {marcacoes(lem.get('marcar', [])) or '(ninguém)'}"]
    await enviar(context.bot, update.effective_chat.id, "\n".join(L))


def main():
    token = (os.getenv("TELEGRAM_TOKEN") or "").strip().strip("`'\" ")
    if not token:
        raise SystemExit("Coloque o TELEGRAM_TOKEN no arquivo .env")
    esperar_telegram(token)
    app = montar_app(token)
    agendar(app)
    log.info("Robô iniciado no modo %s | Tagme: %s", MODO, "ligada" if TG else "desligada")
    # Python 3.14+ não cria mais o "event loop" sozinho; criamos aqui
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


def montar_app(token: str) -> Application:
    """Cria o robô com todos os comandos e botões (usado no PC e nas rodadas do GitHub)."""
    app = (Application.builder().token(token)
           .request(conexao()).get_updates_request(conexao(40)).build())
    app.add_error_handler(erro_geral)
    from telegram.ext import TypeHandler
    app.add_handler(TypeHandler(Update, guardar_pessoa), group=-1)  # guarda nome/@ de quem fala com o robô
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cadastrar", cmd_cadastrar))
    app.add_handler(CommandHandler("pendentes", cmd_pendentes))
    app.add_handler(CommandHandler("agenda", cmd_agenda))
    app.add_handler(CommandHandler("configurar_topico", cmd_configurar_topico))
    app.add_handler(CommandHandler("sou_gestor", cmd_sou_gestor))
    app.add_handler(CallbackQueryHandler(botao_feito, pattern=r"^feito\|"))
    app.add_handler(CommandHandler("configurar_ocorrencias", cmd_configurar_ocorrencias))
    app.add_handler(CommandHandler("pendencias", cmd_pendencias))
    app.add_handler(CommandHandler("configurar_keepbot", cmd_configurar_keepbot))
    app.add_handler(CommandHandler("resolver", cmd_resolver))
    app.add_handler(CommandHandler("excluir", cmd_excluir))
    app.add_handler(CommandHandler("semana", cmd_semana))
    app.add_handler(CommandHandler("reenviar", cmd_reenviar))
    app.add_handler(CommandHandler("testar", cmd_testar))
    app.add_handler(CallbackQueryHandler(botao_teste, pattern=r"^t[tx]\|"))
    app.add_handler(CommandHandler("lembretes", cmd_lembretes))
    app.add_handler(CallbackQueryHandler(botao_reenviar, pattern=r"^re\|"))
    app.add_handler(CommandHandler("fila", cmd_fila))
    app.add_handler(CommandHandler("reservas", cmd_reservas))
    app.add_handler(CommandHandler("configurar_tagme", cmd_configurar_tagme))
    app.add_handler(CallbackQueryHandler(botao_tagme, pattern=r"^tg\|"))
    app.add_handler(CommandHandler("configurar_fechamento", cmd_configurar_fechamento))
    app.add_handler(CommandHandler("testar_lembrete", cmd_testar_lembrete))
    app.add_handler(CallbackQueryHandler(oc_botao, pattern=r"^oca?\|"))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, mensagem_grupo))
    app.add_handler(CommandHandler("alerta", cmd_alerta))
    app.add_handler(CallbackQueryHandler(botao_baixa, pattern=r"^bx\|"))
    app.add_handler(CommandHandler("reativar", cmd_reativar))
    app.add_handler(CallbackQueryHandler(botao_reativar, pattern=r"^ocr\|"))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CallbackQueryHandler(botao_alerta, pattern=r"^al\|"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, msg_privada))
    return app


if __name__ == "__main__":
    main()
