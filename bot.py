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
import re
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
import bar as BAR
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
    usou_agenda = False
    if CFG.get("fonte_leankeep", "agenda") == "agenda":
        # mesma fonte da tela Agenda do site (atualiza bem mais rápido que o Backlog)
        try:
            ativs = []
            for k in range(dias_atras, -1, -1):
                ativs += await asyncio.wait_for(
                    asyncio.to_thread(LK.agenda, d - timedelta(days=k), False, CFG.get("unidade")), timeout=120)
            usou_agenda = True
        except Exception as e:
            log.warning("Agenda do Leankeep falhou (%s); usando o Backlog", e)
    if not usou_agenda:
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
    if ativs and not usou_agenda and CFG.get("cruzar_com_historico", True):
        # o "pendente" do Leankeep às vezes demora a sair mesmo com a baixa já no Histórico:
        # o que já aparece como baixado no Histórico não é cobrado
        try:
            hist = await asyncio.wait_for(
                asyncio.to_thread(LK.baixadas, d - timedelta(days=dias_atras), d, CFG.get("unidade")), timeout=150)
            ja = {a.numero for a in hist if a.numero}
            ja_desc = {(normalizar(a.descricao), a.data[:10]) for a in hist}
            antes = len(ativs)
            ativs = [a for a in ativs if not ((a.numero and a.numero in ja)
                                              or (normalizar(a.descricao), a.data[:10]) in ja_desc)]
            if len(ativs) != antes:
                log.info("Leankeep: %s atividade(s) 'pendente(s)' já estavam no Histórico como baixadas",
                         antes - len(ativs))
        except Exception as e:
            log.warning("Não consegui cruzar com o Histórico do Leankeep: %s", e)
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
    if cob.get("so_gestor"):
        if rodada == 0:  # só uma mensagem, no horário do aviso, no privado do gestor
            await cobrar_so_gestor(bot, cob)
        return
    if declarada(cob) and not TESTE.get():
        log.info("Cobrança de %s não enviada: baixa declarada por %s", cob["nome"], declarada(cob)["quem"])
        return
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


async def cobrar_so_gestor(bot, cob):
    """Cobrança que vai SÓ para o privado do gestor (ex.: Conferência da diretoria)."""
    g = CFG.get("gestor_chat_id")
    if not g:
        return
    try:
        ativs = sem_duplicadas(await buscar_pendentes(cob.get("palavras"), data_ref(cob), cob.get("excluir")))
    except Exception as e:
        await enviar(bot, g, f"⚠️ Não consegui consultar o Leankeep para <b>{esc(cob['nome'])}</b>: {esc(e)}")
        return
    if not ativs:
        await enviar(bot, g, f"✅ <b>{esc(cob['nome'])}</b>: tudo baixado no Leankeep.")
        return
    linhas = [f"🚨🚨 <b>{esc(cob['nome'].upper())}</b> 🚨🚨", "",
              f"🔴 {len(ativs)} atividade(s) ainda sem baixa no Leankeep · prazo {esc(txt_prazo(cob))}"]
    linhas += bloco_atividades(ativs, cadastros(), limite=30)
    await enviar(bot, g, "\n".join(linhas))


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
    jq.run_repeating(verificar_declaradas, interval=900, first=120, name="verificar_declaradas")
    jq.run_repeating(BAR.tick, interval=300, first=90, name="bar")
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
    if card_cfg().get("ativo", True):
        horarios = card_cfg().get("horarios") or [card_cfg().get("horario", "09:00")]
        for i, h in enumerate(horarios):  # ex.: 10:00 e 18:00 – cada horário compara com a conferência anterior
            lst.append(("cardapio" if i == 0 else f"cardapio_{h.replace(':', '')}", cardapio_diario, hora(h),
                        TODOS_DIAS, 6))
    if aud_cfg().get("ativo", True) and aud_cfg().get("lembrete_semanal", True) and MODO == "leankeep":
        dia_txt, hora_txt = str(aud_cfg().get("quando", "seg 09:30")).split()
        lst.append(("auditoria_semanal", auditoria_semanal, hora(hora_txt), ((DIAS[dia_txt] + 1) % 7,), 12))
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


# Raízes das palavras: "quebr" pega quebra, quebrou, quebrado… (maiúsculas e acentos não importam)
PALAVRAS_ABREM_PADRAO = ["ocorrencia", "manutencao", "danific", "quebr", "vaza", "queim", "estrag", "defeit",
                         "entup", "consert", "concert", "pifou", "pifad", "nao funciona", "nao esta funcionando",
                         "sem funcionar"]
PALAVRAS_PERGUNTAM_PADRAO = ["verific", "avali", "revis", "troca", "solt", "parou", "parad"]


def _tem_raiz(texto: str, raizes: list[str]) -> bool:
    t = " " + " ".join(re.sub(r"[^A-Z0-9 ]", " ", normalizar(texto)).split())
    for r in raizes:
        r = " ".join(re.sub(r"[^A-Z0-9 ]", " ", normalizar(r)).split())
        if r and f" {r}" in t:  # começo de palavra: "parou" não pega "separou"
            return True
    return False


def oc_nivel(texto: str) -> str | None:
    """'abre' = vira ocorrência; 'pergunta' = robô pergunta com botão; None = só conversa."""
    if not oc_cfg().get("exigir_palavra_chave", True):
        return "abre"
    cfg = oc_cfg()
    if _tem_raiz(texto, cfg.get("palavras_abrem", cfg.get("palavras_chave", PALAVRAS_ABREM_PADRAO))):
        return "abre"
    if _tem_raiz(texto, cfg.get("palavras_perguntam", PALAVRAS_PERGUNTAM_PADRAO)):
        return "pergunta"
    return None


def eh_resolvido(texto: str) -> bool:
    """Mensagem de solução ("Trocado ✅", "resolvido", "consertado"). Vale o começo da palavra
    ("conserta" pega consertado/consertada) e os emojis de check."""
    if any(e in (texto or "") for e in oc_cfg().get("emojis_resolvido", ["✅", "✔", "☑"])):
        return True
    palavras = oc_cfg().get("palavras_resolvido",
                            ["resolvid", "concluid", "finalizad", "trocad", "consertad", "arrumad", "reparad"])
    return _tem_raiz(texto, palavras)


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


class _Enviada:
    def __init__(self, chat_id, message_id):
        self.chat_id, self.message_id = chat_id, message_id


async def oc_enviar_com_foto(bot, chat, thread, o: dict, texto: str, kb=None):
    """Manda a ocorrência com a FOTO aberta (cópia da foto original com o texto embaixo).
    Sem foto, ou se a cópia falhar, manda só o texto."""
    if not chat:
        return None
    if o.get("foto") and o.get("msg_ids"):
        destino, kb2, legenda = chat, kb, limpar_texto(texto)
        quem_testa = TESTE.get()
        if quem_testa:
            legenda += f"\n\n<i>TESTE – no dia a dia vai para: {esc(nome_destino(chat, thread))}</i>"
            destino, thread, kb2 = quem_testa, None, teclado_de_teste(kb) if kb else None
        if len(legenda) > 1000:
            legenda = legenda[:990] + "…"
        try:
            r = await bot.copy_message(destino, o["chat_id"], o["msg_ids"][0], caption=legenda,
                                       parse_mode=ParseMode.HTML, message_thread_id=thread or None,
                                       **({"reply_markup": kb2} if kb2 else {}))
            return _Enviada(destino, r.message_id)
        except Exception as e:
            log.warning("Não consegui copiar a foto da ocorrência #%s: %s", o.get("num"), e)
    r = await enviar(bot, chat, texto, message_thread_id=thread or None, **({"reply_markup": kb} if kb else {}))
    return _Enviada(r.chat.id if hasattr(r, "chat") else chat, r.message_id) if r is not None else None


def oc_lembrar_msg(num, enviada):
    """Guarda as mensagens do robô sobre a ocorrência no grupo, para apagar quando for concluída."""
    if enviada is None or TESTE.get() or int(enviada.chat_id) > 0:
        return
    dados = oc_dados()
    o = dados["itens"].get(str(num))
    if o is not None:
        lista = o.setdefault("robo_msgs", [])
        if [int(enviada.chat_id), enviada.message_id] not in lista:
            lista.append([int(enviada.chat_id), enviada.message_id])
            gravar(ARQ_OCORR, dados)


async def oc_limpar_grupo(bot, num, extra=None):
    """Concluída: apaga do grupo as mensagens do robô sobre ela (registro, cartões, avisos)."""
    dados = oc_dados()
    o = dados["itens"].get(str(num))
    if not o:
        return
    msgs = list(o.get("robo_msgs", []))
    if o.get("msg_robo") and saida_separada():
        c, _ = oc_saida()
        msgs.append([int(c), o["msg_robo"]])
    elif o.get("msg_robo"):
        msgs.append([int(o["chat_id"]), o["msg_robo"]])
    if extra:
        msgs.append(list(extra))
    feitos = set()
    for chat, mid in msgs:
        # no privado só apaga o cartão em que o botão foi tocado (o backup chega logo em seguida)
        if (chat, mid) in feitos or (int(chat) > 0 and not (extra and [chat, mid] == list(extra))):
            continue
        feitos.add((chat, mid))
        try:
            await bot.delete_message(chat, mid)
        except Exception as e:  # sem permissão de apagar: pelo menos tira os botões
            log.info("Não consegui apagar a mensagem %s da ocorrência #%s: %s", mid, num, e)
            aviso = f"✅ Ocorrência #{num} concluída"
            try:
                await bot.edit_message_caption(chat, mid, caption=aviso, reply_markup=None)
            except Exception:
                try:
                    await bot.edit_message_text(aviso, chat, mid, reply_markup=None)
                except Exception:
                    pass
    o["robo_msgs"] = []
    o.pop("msg_robo", None)
    gravar(ARQ_OCORR, dados)


async def oc_concluida(bot, num, extra=None):
    """Depois de concluir: backup no privado do gestor e limpeza do grupo."""
    await oc_baixa_privado(bot, num)
    if oc_cfg().get("apagar_ao_concluir", True):
        await oc_limpar_grupo(bot, num, extra)


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
    await oc_enviar_com_foto(bot, int(g), None, o, "\n".join(L), teclado)
    sol = o.get("solucao")
    if sol and sol.get("foto"):
        try:
            await bot.copy_message(int(g), sol["chat_id"], sol["msg_id"],
                                   caption=f"🛠️ Foto da solução – Ocorrência #{num}"
                                           + (f" (postada por {sol['por']})" if sol.get("por") else ""))
        except Exception as e:
            log.info("Não consegui copiar a foto da solução da #%s: %s", num, e)
    lk = o.get("leankeep") or {}
    if lk.get("numero") and lk.get("status") != "Solucionada" and seg_cfg().get("ativo", True):
        tem_foto = bool((o.get("solucao") or {}).get("foto_file"))
        await enviar(bot, int(g),
                     f"📤 A #{num} está no Leankeep como <b>{esc(lk['numero'])}</b>. Dar baixa lá também (Solucionada)?"
                     + ("" if tem_foto else "\n<i>O Leankeep exige a foto da resolução: no próximo passo eu peço.</i>"),
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                         "✅ Baixar no Leankeep", callback_data=f"sg|c|{num}|{legado_status('Solucionada')}"),
                         InlineKeyboardButton("Agora não", callback_data="sg|x")]]))


async def oc_avisar_reativada(bot, num, quem):
    o = oc_dados()["itens"].get(str(num))
    if not o:
        return
    c, t = oc_saida()
    resumo = esc((o.get("texto") or "(só foto)")[:300])
    r = await oc_enviar_com_foto(bot, c, t or None, o,
                                 f"↩️ <b>Ocorrência #{num} reativada</b> – volta para 🔴 Pendente\n\n"
                                 f"{resumo}\n<i>Reativada por {esc(quem)} · {datetime.now(FUSO):%d/%m %H:%M}</i>"
                                 f" · <a href=\"{oc_link(o)}\">↗ ver no grupo</a>",
                                 botao_oc(num, o))
    oc_lembrar_msg(num, r)


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
        nota = f"\n\n<b>Reativada às {datetime.now(FUSO):%H:%M} – voltou para Pendente.</b>"
        if q.message.photo:
            await q.edit_message_caption(caption=limpar_texto((q.message.caption_html or "") + nota)[:1024],
                                         parse_mode=ParseMode.HTML)
        else:
            await q.edit_message_text(limpar_texto((q.message.text_html or q.message.text or "") + nota),
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
                if msg.photo:
                    dados["itens"][num]["solucao"] = {"chat_id": msg.chat.id, "msg_id": msg.message_id,
                                                      "foto": True, "por": quem,
                                                      "foto_file": msg.photo[-1].file_id}
                    gravar(ARQ_OCORR, dados)
                await reagir(context.bot, msg, "👍")  # confirma sem poluir o grupo
                await oc_concluida(context.bot, num)
        elif num and eh_andamento(texto):
            if oc_andamento(dados, num, quem):
                r = await oc_avisar(context.bot, msg, f"🟡 Ocorrência #{num} em andamento ({esc(quem)}).",
                                    reply_markup=botao_oc(num, dados["itens"][num]))
                if r is not None:
                    oc_lembrar_msg(num, _Enviada(r.chat.id, r.message_id))
        elif num and pede_abrir(texto):  # pediu para abrir, mas essa foto já é uma ocorrência
            o = dados["itens"][num]
            r = await enviar(context.bot, msg.chat.id,
                             f"📌 Essa foto já está registrada como <b>Ocorrência #{num}</b> – {oc_situacao(o)}",
                             message_thread_id=msg.message_thread_id if msg.is_topic_message else None,
                             reply_to_message_id=msg.message_id,
                             **({"reply_markup": botao_oc(num, o)} if not o.get("resolvida") else {}))
            if r is not None and not o.get("resolvida"):
                oc_lembrar_msg(num, _Enviada(r.chat.id, r.message_id))
        elif not num and pede_abrir(texto) and no_topico_ocorrencias(msg) and not no_topico_saida(msg) \
                and not (alvo.from_user and alvo.from_user.is_bot):
            await oc_abrir_por_resposta(context.bot, dados, msg, alvo, quem)
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

    if eh_resolvido(texto):
        # foto/texto mostrando a solução ("Trocado ✅") não abre ocorrência. Com foto e havendo
        # ocorrências em aberto, o robô pergunta qual delas foi concluída.
        if tem_midia and oc_cfg().get("perguntar_conclusao", True) and any(
                not o.get("resolvida") for o in dados["itens"].values()):
            chave_p = f"{msg.chat.id}:{msg.message_id}"
            dados.setdefault("perguntas", {})[chave_p] = {
                "chat_id": msg.chat.id, "topico": msg.message_thread_id if msg.is_topic_message else 0,
                "msg_ids": [msg.message_id], "album": msg.media_group_id, "autor": quem, "autor_id": uid,
                "texto": texto, "foto": bool(msg.photo), "criada": enviada.isoformat(timespec="minutes"),
                "ultima_msg": enviada.isoformat(timespec="seconds"), "resolvida": None}
            gravar(ARQ_OCORR, dados)
            r = await enviar(context.bot, msg.chat.id,
                             f"✅ {esc(quem.split()[0])}, qual ocorrência foi concluída?",
                             message_thread_id=msg.message_thread_id if msg.is_topic_message else None,
                             reply_to_message_id=msg.message_id, reply_markup=teclado_conclusao(chave_p))
            if r is not None:
                dados = oc_dados()
                if chave_p in dados.get("perguntas", {}):
                    dados["perguntas"][chave_p]["msg_pergunta"] = r.message_id
                    gravar(ARQ_OCORR, dados)
        return

    # 4) palavra-chave. Palavra "forte" (quebrou, vazando…) abre direto; palavra "fraca"
    #    (verificar, avaliar…) faz o robô perguntar com botão; sem palavra, não vira ocorrência.
    #    A foto mandada pouco antes pela mesma pessoa fica guardada para entrar junto.
    cands = {k: c for k, c in dados.get("candidatas", {}).items()
             if abs((enviada - datetime.fromisoformat(c["ultima_msg"])).total_seconds()) <= janela}
    chave_c = f"{msg.chat.id}:{uid}"
    cand = cands.get(chave_c)
    nivel = oc_nivel(texto)
    if not nivel:
        if cand:
            cand["msg_ids"].append(msg.message_id)
            cand["texto"] = (cand["texto"] + " " + texto).strip() if texto else cand["texto"]
            cand["foto"] = cand["foto"] or bool(msg.photo)
            cand["foto_file"] = cand.get("foto_file") or (msg.photo[-1].file_id if msg.photo else None)
            cand["album"] = cand.get("album") or msg.media_group_id
            cand["ultima_msg"] = enviada.isoformat(timespec="seconds")
        else:
            cands[chave_c] = {"msg_ids": [msg.message_id], "texto": texto, "foto": bool(msg.photo),
                              "foto_file": msg.photo[-1].file_id if msg.photo else None,
                              "album": msg.media_group_id,
                              "criada": enviada.isoformat(timespec="minutes"),
                              "ultima_msg": enviada.isoformat(timespec="seconds")}
        dados["candidatas"] = cands
        gravar(ARQ_OCORR, dados)
        # foto nova sem palavra-chave: o robô pergunta com botões (abrir / conclusão / não é ocorrência)
        if tem_midia and oc_cfg().get("perguntar_toda_foto", True):
            pergs = dados.setdefault("perguntas", {})
            mesma = next((pg for pg in pergs.values() if pg.get("chat_id") == msg.chat.id and (
                (msg.media_group_id and pg.get("album") == msg.media_group_id)
                or (pg.get("autor_id") == uid and abs((enviada - datetime.fromisoformat(
                    pg.get("ultima_msg", pg["criada"]))).total_seconds()) <= janela))), None)
            if mesma:  # outra foto do mesmo álbum / da mesma pessoa logo em seguida: entra na mesma pergunta
                mesma["msg_ids"].append(msg.message_id)
                mesma["foto"] = mesma.get("foto") or bool(msg.photo)
                if texto and not mesma.get("texto"):
                    mesma["texto"] = texto
                mesma["ultima_msg"] = enviada.isoformat(timespec="seconds")
                gravar(ARQ_OCORR, dados)
                return
            c = cands[chave_c]
            chave_p = f"{msg.chat.id}:{msg.message_id}"
            pergs[chave_p] = {"chat_id": msg.chat.id, "topico": msg.message_thread_id if msg.is_topic_message else 0,
                              "msg_ids": list(c["msg_ids"]), "album": c.get("album"), "autor": quem, "autor_id": uid,
                              "texto": c.get("texto", ""), "foto": c.get("foto"), "foto_file": c.get("foto_file"),
                              "criada": c["criada"],
                              "ultima_msg": enviada.isoformat(timespec="seconds"), "resolvida": None}
            gravar(ARQ_OCORR, dados)
            r = await enviar(context.bot, msg.chat.id, f"📷 {esc(quem.split()[0])}, do que se trata esta foto?",
                             message_thread_id=msg.message_thread_id if msg.is_topic_message else None,
                             reply_to_message_id=msg.message_id, reply_markup=teclado_pergunta(chave_p))
            if r is not None:
                dados = oc_dados()
                if chave_p in dados.get("perguntas", {}):
                    dados["perguntas"][chave_p]["msg_pergunta"] = r.message_id
                    gravar(ARQ_OCORR, dados)
        return
    if nivel == "abre" and not oc_cfg().get("exigir_palavra_chave", True) and not tem_midia \
            and len(texto) < int(oc_cfg().get("min_caracteres_texto", 15)):
        return  # "bom dia", "ok"… não viram ocorrência

    ids, foto, criada, album = [msg.message_id], bool(msg.photo), enviada.isoformat(timespec="minutes"), msg.media_group_id
    foto_file = msg.photo[-1].file_id if msg.photo else None
    if cand:  # junta a foto (ou texto) mandada pouco antes, sem palavra-chave
        cands.pop(chave_c)
        # a pergunta "do que se trata esta foto?" já não precisa: o texto com a palavra-chave respondeu
        for kp, pg in list(dados.get("perguntas", {}).items()):
            if pg.get("chat_id") == msg.chat.id and set(pg.get("msg_ids", [])) & set(cand["msg_ids"]):
                dados["perguntas"].pop(kp)
                if pg.get("msg_pergunta"):
                    try:
                        await context.bot.delete_message(msg.chat.id, pg["msg_pergunta"])
                    except Exception:
                        pass
        ids = cand["msg_ids"] + ids
        texto = (cand["texto"] + " " + texto).strip() if cand["texto"] else texto
        criada, album = cand["criada"], cand.get("album") or album
        foto_file = cand.get("foto_file") or foto_file
        if cand["foto"]:
            foto = True
        elif msg.photo:
            ids.remove(msg.message_id)
            ids.insert(0, msg.message_id)
    dados["candidatas"] = cands
    item = {"chat_id": msg.chat.id, "topico": msg.message_thread_id if msg.is_topic_message else 0,
            "msg_ids": ids, "album": album, "autor": quem, "autor_id": uid, "texto": texto,
            "foto": foto, "foto_file": foto_file, "criada": criada, "ultima_msg": enviada.isoformat(timespec="seconds"),
            "resolvida": None}

    if nivel == "pergunta":
        chave_p = f"{msg.chat.id}:{msg.message_id}"
        dados.setdefault("perguntas", {})[chave_p] = item
        gravar(ARQ_OCORR, dados)
        r = await enviar(context.bot, msg.chat.id,
                         f"🤔 {esc(quem.split()[0])}, do que se trata esta postagem?",
                         message_thread_id=item["topico"] or None, reply_to_message_id=msg.message_id,
                         reply_markup=teclado_pergunta(chave_p))
        if r is not None:
            dados = oc_dados()
            if chave_p in dados.get("perguntas", {}):
                dados["perguntas"][chave_p]["msg_pergunta"] = r.message_id
                gravar(ARQ_OCORR, dados)
        return

    await oc_registrar(context.bot, dados, item)


def pede_abrir(texto: str) -> bool:
    """Resposta (marcando a foto) pedindo para abrir: "abrir ocorrência", "isso é ocorrência"…"""
    t = " " + " ".join(re.sub(r"[^A-Z0-9 ]", " ", normalizar(texto)).split())
    if any(n in t for n in (" NAO E OCORR", " NAO EH OCORR", " NAO VIRA OCORR", " NAO PRECISA",
                            " NAO ABR", " SEM OCORR")):
        return False
    frases = oc_cfg().get("frases_abrir", ["ocorr", "abrir chamado", "abre chamado", "abrir os", "abre os"])
    return any(" " + " ".join(re.sub(r"[^A-Z0-9 ]", " ", normalizar(f)).split()) in t for f in frases)


async def oc_abrir_por_resposta(bot, dados: dict, msg, alvo, quem: str):
    """Alguém marcou (respondeu) a foto/mensagem e pediu para abrir: vira ocorrência mesmo sem palavra-chave."""
    chat = msg.chat.id
    # a foto pode estar guardada como "candidata" (com outras fotos/texto da mesma pessoa) ou numa pergunta
    base = None
    for k, c in list(dados.get("candidatas", {}).items()):
        if alvo.message_id in c["msg_ids"] and k.startswith(f"{chat}:"):
            base = dados["candidatas"].pop(k)
            break
    for k, pg in list(dados.get("perguntas", {}).items()):
        if alvo.message_id in pg["msg_ids"] and pg["chat_id"] == chat:
            base = dados["perguntas"].pop(k)
            if pg.get("msg_pergunta"):
                try:
                    await bot.delete_message(chat, pg["msg_pergunta"])
                except Exception:
                    pass
            break
    autor = alvo.from_user.full_name if alvo.from_user else quem
    enviada = alvo.date.astimezone(FUSO) if alvo.date else datetime.now(FUSO)
    texto_alvo = (alvo.text or alvo.caption or "").strip()
    if base:
        ids, texto_alvo, foto, criada, album = (base["msg_ids"], base.get("texto") or texto_alvo,
                                                base.get("foto"), base["criada"], base.get("album"))
    else:
        ids, foto, criada, album = ([alvo.message_id], bool(alvo.photo),
                                    enviada.isoformat(timespec="minutes"), alvo.media_group_id)
    pedido = (msg.text or msg.caption or "").strip()
    texto = texto_alvo or pedido  # foto sem legenda: usa o que foi escrito na resposta
    if texto_alvo and len(pedido) > 25:
        texto = f"{texto_alvo} – {pedido}"
    item = {"chat_id": chat, "topico": msg.message_thread_id if msg.is_topic_message else 0,
            "msg_ids": ids, "album": album, "autor": autor,
            "autor_id": alvo.from_user.id if alvo.from_user else None, "texto": texto, "foto": bool(foto),
            "foto_file": (base or {}).get("foto_file") or (alvo.photo[-1].file_id if alvo.photo else None),
            "criada": criada, "ultima_msg": enviada.isoformat(timespec="seconds"), "resolvida": None}
    if autor != quem:
        item["aberta_por"] = quem
    await oc_registrar(bot, dados, item)


async def oc_registrar(bot, dados: dict, item: dict) -> str:
    """Dá número à ocorrência, grava e confirma (no Keepbot ou respondendo a própria foto)."""
    dados["seq"] += 1
    num = str(dados["seq"])
    item["num"] = int(num)
    dados["itens"][num] = item
    gravar(ARQ_OCORR, dados)
    if not oc_cfg().get("confirmar_registro", True):
        return num
    quem, texto = item["autor"], item["texto"]
    if item.get("aberta_por"):
        quem += f" (aberta por {item['aberta_por']})"
    if saida_separada():
        resumo = esc(texto[:300] + ("…" if len(texto) > 300 else "")) if texto else "(só foto)"
        link = f"<a href=\"{oc_link(item)}\">{'↗ ver no grupo' if item.get('foto') else '↗ ver mensagem'}</a>"
        c, t = oc_saida()
        r = await oc_enviar_com_foto(bot, c, t or None, item,
                                     f"📌 <b>Ocorrência #{num} registrada</b> – 🔴 Pendente\n\n"
                                     f"{resumo}\n"
                                     f"<i>{esc(quem)} · {datetime.fromisoformat(item['criada']):%d/%m %H:%M}</i> · {link}",
                                     botao_oc(num))
    else:
        r = await enviar(bot, item["chat_id"],
                         f"📌 Ocorrência #{num} registrada – 🔴 Pendente\n\n"
                         f"Use os botões abaixo (ou responda a foto com \"em andamento\" ou \"resolvido\").",
                         message_thread_id=item["topico"] or None, reply_to_message_id=item["msg_ids"][0],
                         reply_markup=botao_oc(num))
    if r is not None:
        dados = oc_dados()
        dados["itens"][num]["msg_robo"] = r.message_id
        gravar(ARQ_OCORR, dados)
    await lk_oferecer(bot, num)
    return num


# =====================================================================
#  Registrar a ocorrência no Leankeep (botões no privado do gestor)
# =====================================================================
def lk_reg_cfg() -> dict:
    return CFG.get("leankeep_registro", {}) or {}


PRIOR_COD = {"A": "Alta", "M": "Média", "B": "Baixa"}


def lk_areas() -> dict:
    """Grupos e áreas da unidade (lidos pelo /testar_leankeep): {gid: (nome, [(aid, nome), ...])}"""
    res = (ler(ARQ_LK_AREAS, {}) or {}).get("grupos") or {}
    out = {}
    for chave, areas in res.items():
        gid, nome = chave.split("|", 1)
        if isinstance(areas, list):
            out[gid] = (nome, [tuple(a) for a in areas])
    return out


async def lk_oferecer(bot, num, chat=None):
    """Cartão da ocorrência no privado do gestor com o botão Registrar no Leankeep."""
    g = chat or CFG.get("gestor_chat_id")
    if not g or not lk_reg_cfg().get("ativo", True) or MODO != "leankeep":
        return
    o = oc_dados()["itens"].get(str(num))
    if not o:
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Registrar no Leankeep", callback_data=f"lk|g|{num}"),
                                InlineKeyboardButton("Agora não", callback_data=f"lk|x|{num}")]])
    texto = (f"🆕 <b>Ocorrência #{num}</b> – {esc(o.get('autor', '?'))}\n\n{esc((o.get('texto') or '(só foto)')[:500])}\n\n"
             f"<i>Quer registrar no Leankeep?</i>")
    await oc_enviar_com_foto(bot, int(g), None, o, texto, kb)


def _kb_lk(linhas, num):
    linhas = [l for l in linhas if l]
    linhas.append([InlineKeyboardButton("✖️ Cancelar", callback_data=f"lk|x|{num}")])
    return InlineKeyboardMarkup(linhas)


async def lk_botao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _eh_gestor(update):
        await q.answer("Só o gestor registra no Leankeep.", show_alert=True)
        return
    p = q.data.split("|")
    passo, num = p[1], p[2]
    o = oc_dados()["itens"].get(num)
    if not o:
        await q.answer("Ocorrência não encontrada.")
        return
    titulo = f"📤 <b>Registrar a ocorrência #{num} no Leankeep</b>\n{esc((o.get('texto') or '(só foto)')[:200])}\n\n"

    async def mostrar(txt, kb):
        if passo == "g":  # 1º passo: mensagem nova (o cartão com foto fica como está)
            await enviar(context.bot, q.message.chat.id, titulo + txt, reply_markup=kb)
            try:
                await q.edit_message_reply_markup(None)
            except Exception:
                pass
        else:
            try:
                await q.edit_message_text(limpar_texto(titulo + txt), parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                await enviar(context.bot, q.message.chat.id, titulo + txt, reply_markup=kb)

    if passo == "x":
        await q.answer("Ok.")
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
        return
    if o.get("leankeep") and not str(o["leankeep"].get("numero", "")).startswith("SALVA") and passo in ("g", "s"):
        await q.answer(f"Já registrada no Leankeep: {o['leankeep'].get('numero')}", show_alert=True)
        return
    areas = lk_areas()
    if not areas:
        await q.answer("Primeiro rode /testar_leankeep (carrega os grupos e áreas).", show_alert=True)
        return
    await q.answer()
    if passo == "g":
        bts = [InlineKeyboardButton(nome.title(), callback_data=f"lk|a|{num}|{gid}") for gid, (nome, _) in areas.items()]
        await mostrar("<b>1/3 – Grupo de áreas?</b>", _kb_lk([bts[i:i + 2] for i in range(0, len(bts), 2)], num))
    elif passo == "a":
        gid = p[3]
        nome_g, lst = areas.get(gid, ("?", []))
        bts = [InlineKeyboardButton(n.split("/")[0].title()[:30], callback_data=f"lk|p|{num}|{gid}|{aid}") for aid, n in lst]
        await mostrar(f"Grupo: <b>{esc(nome_g.title())}</b>\n\n<b>2/3 – Área?</b>",
                      _kb_lk([bts[i:i + 2] for i in range(0, len(bts), 2)], num))
    elif passo == "p":
        gid, aid = p[3], p[4]
        nome_g, lst = areas.get(gid, ("?", []))
        nome_a = next((n for a, n in lst if a == aid), "?")
        bts = [InlineKeyboardButton(f"{e} {PRIOR_COD[c]}", callback_data=f"lk|c|{num}|{gid}|{aid}|{c}")
               for c, e in (("A", "🔴"), ("M", "🟡"), ("B", "🟢"))]
        await mostrar(f"Grupo: <b>{esc(nome_g.title())}</b> · Área: <b>{esc(nome_a.split('/')[0].title())}</b>\n\n"
                      f"<b>3/3 – Prioridade?</b>", _kb_lk([bts], num))
    elif passo == "c":
        gid, aid, pr = p[3], p[4], p[5]
        nome_g, lst = areas.get(gid, ("?", []))
        nome_a = next((n for a, n in lst if a == aid), "?")
        criada = datetime.fromisoformat(o["criada"])
        resumo = (f"<b>Confira antes de enviar:</b>\n"
                  f"• Unidade: CB JUNDIAÍ\n• Grupo: <b>{esc(nome_g.title())}</b>\n"
                  f"• Área: <b>{esc(nome_a.split('/')[0].title())}</b>\n• Prioridade: <b>{PRIOR_COD[pr]}</b>\n"
                  f"• Data/hora: {criada:%d/%m/%Y %H:%M}\n• Foto: {'sim' if o.get('foto_file') else 'não'}\n"
                  f"• Descrição: {esc((o.get('texto') or '(só foto)')[:300])}")
        if not aud_cfg().get("ativo", True):
            await mostrar(resumo, _kb_lk([[InlineKeyboardButton("✅ Registrar no Leankeep",
                                                                callback_data=f"lk|s|{num}|{gid}|{aid}|{pr}|0")]], num))
            return
        await mostrar(resumo + "\n\n⏳ <i>Conferindo a auditoria (batidão) da semana…</i>", None)
        try:
            sit = await aud_situacao()
        except Exception as e:
            log.warning("Auditoria: consulta falhou: %s", e)
            sit = None
        if sit is None:
            linha = "• Auditoria: <i>não consegui consultar – dá para registrar avulsa</i>"
            bts = [[InlineKeyboardButton("✅ Registrar avulsa", callback_data=f"lk|s|{num}|{gid}|{aid}|{pr}|0")]]
        elif sit["atual"]:
            a = sit["atual"]
            linha = f"• Auditoria: <b>{esc(a['nome'])}</b> ({_dm(a['data'])}, nº {a['id']})"
            bts = [[InlineKeyboardButton(f"✅ Registrar na {a['nome']}", callback_data=f"lk|s|{num}|{gid}|{aid}|{pr}|A")],
                   [InlineKeyboardButton("Registrar avulsa (fora da auditoria)", callback_data=f"lk|s|{num}|{gid}|{aid}|{pr}|0")]]
        else:
            linha = f"• Auditoria: <i>ainda não há uma desta semana</i>"
            bts = [[InlineKeyboardButton(f"🆕 Abrir {sit['proximo']} e registrar nela",
                                         callback_data=f"lk|s|{num}|{gid}|{aid}|{pr}|N")],
                   [InlineKeyboardButton("Registrar avulsa (fora da auditoria)", callback_data=f"lk|s|{num}|{gid}|{aid}|{pr}|0")]]
        await mostrar(resumo + "\n" + linha, _kb_lk(bts, num))
    elif passo == "s":
        gid, aid, pr = p[3], p[4], p[5]
        nome_g, lst = areas.get(gid, ("?", []))
        nome_a = next((n for a, n in lst if a == aid), "?")
        modo_aud = p[6] if len(p) > 6 else "0"
        auditoria = None
        if modo_aud == "A":
            auditoria = (ler(ARQ_AUD, {}) or {}).get("atual")
        elif modo_aud == "N":
            nome_aud = (ler(ARQ_AUD, {}) or {}).get("proximo") or "Auditoria 1"
            await mostrar(f"⏳ Abrindo a {esc(nome_aud)} no Leankeep…", None)
            try:
                auditoria = await aud_criar(nome_aud, hoje())
            except Exception as e:
                log.exception("Criar auditoria falhou")
                await enviar(context.bot, q.message.chat.id,
                             f"⚠️ <b>Não consegui abrir a {esc(nome_aud)}.</b>\n{esc(str(e)[:400])}",
                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                 "🔁 Tentar de novo", callback_data=f"lk|c|{num}|{gid}|{aid}|{pr}")]]))
                return
            await enviar(context.bot, q.message.chat.id,
                         f"✅ <b>{esc(auditoria['nome'])} aberta no Leankeep – nº {auditoria['id']}</b>")
        if modo_aud in ("A", "N") and not auditoria:
            await enviar(context.bot, q.message.chat.id, "⚠️ Perdi a referência da auditoria. Confira de novo:",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                             "🔁 Conferir de novo", callback_data=f"lk|c|{num}|{gid}|{aid}|{pr}")]]))
            return
        await mostrar("⏳ Registrando no Leankeep… (leva até 1 minuto)"
                      + (f"\nDentro da {esc(auditoria['nome'])}" if auditoria else ""), None)
        foto = None
        if o.get("foto_file"):
            try:
                f = await context.bot.get_file(o["foto_file"])
                foto = bytes(await f.download_as_bytearray())
            except Exception as e:
                log.warning("Leankeep: não consegui baixar a foto da #%s: %s", num, e)
        desc = (o.get("texto") or "Ocorrência registrada pelo Telegram").strip()
        desc += f"\n(Registrada pelo Telegram – ocorrência #{num}, por {o.get('autor', '?')})"
        import legado

        def rodar():
            # acesso caiu (ex.: o mesmo usuário entrou no navegador): renova e tenta UMA vez de novo.
            # Só antes de salvar: erros do Salvar ("não salvou") não repetem, para não duplicar.
            return _lk_chamar(legado.registrar, (gid, nome_g), (aid, nome_a), PRIOR_COD[pr], desc, foto,
                              datetime.fromisoformat(o["criada"]).replace(tzinfo=None), auditoria=auditoria)
        try:
            numero = await asyncio.wait_for(asyncio.to_thread(rodar), 240)
        except Exception as e:
            log.exception("Registro no Leankeep falhou")
            await enviar(context.bot, q.message.chat.id,
                         f"⚠️ <b>Não consegui registrar a #{num} no Leankeep.</b>\n{esc(str(e)[:400])}\n\n"
                         f"<i>Nada foi salvo pela metade? Confira na Lista de Ocorrências antes de tentar de novo.</i>",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                             "🔁 Tentar de novo", callback_data=f"lk|c|{num}|{gid}|{aid}|{pr}")]]))
            return
        numero, _, aviso_foto = numero.partition("|")
        dados = oc_dados()
        dados["itens"][num]["leankeep"] = {"numero": numero, "em": datetime.now(FUSO).isoformat(timespec="minutes"),
                                           "grupo": nome_g, "area": nome_a, "prioridade": PRIOR_COD[pr],
                                           "auditoria": (auditoria or {}).get("nome")}
        gravar(ARQ_OCORR, dados)
        await enviar(context.bot, q.message.chat.id,
                     f"✅ <b>Ocorrência #{num} registrada no Leankeep: {esc(numero)}</b>\n"
                     f"{esc(nome_g.title())} › {esc(nome_a.split('/')[0].title())} · prioridade {PRIOR_COD[pr]}"
                     + (f"\nDentro da <b>{esc(auditoria['nome'])}</b> (nº {auditoria['id']})" if auditoria else "")
                     + (f"\n\n⚠️ <i>Foi registrada {esc(aviso_foto[:250])}. Anexe a foto pelo Leankeep, se precisar.</i>"
                        if aviso_foto else ""),
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                         "🔧 Dar seguimento", callback_data=f"sg|m|{num}")]]))


# =====================================================================
#  Auditoria (batidão) no Leankeep: "Auditoria N" e ocorrências dentro dela
# =====================================================================
ARQ_AUD = DADOS / "auditoria.json"


def aud_cfg() -> dict:
    return CFG.get("auditoria", {}) or {}


def _lk_chamar(fn, *a, **kw):
    """Chama o Leankeep antigo; se o acesso caiu (mesmo usuário entrou no navegador), renova e tenta 1x."""
    if not LK.token:
        LK.login()
    try:
        return fn(LK.token, LK.refresh, *a, empresa_id=LK.empresa_id, **kw)
    except RuntimeError as e:
        if not any(t in str(e) for t in ("não aceitou o acesso", "não achei o formulário", "logout")):
            raise
        log.warning("Leankeep antigo: acesso caiu, renovando: %s", e)
        try:
            LK.renovar()
        except Exception:
            LK.login()
        return fn(LK.token, LK.refresh, *a, empresa_id=LK.empresa_id, **kw)


async def aud_situacao() -> dict:
    """Consulta a lista do Leankeep: auditoria da semana (atual) e o próximo nome livre do mês."""
    import legado
    lista = await asyncio.wait_for(asyncio.to_thread(_lk_chamar, legado.listar_auditorias), 120)
    sit = legado.situacao_auditoria(lista, hoje(), int(aud_cfg().get("dias_validade", 6)))
    d = ler(ARQ_AUD, {})
    d.update({"atual": sit["atual"], "proximo": sit["proximo"], "consultado": datetime.now(FUSO).isoformat(timespec="minutes")})
    gravar(ARQ_AUD, d)
    return sit


async def aud_criar(nome: str, data: date) -> dict:
    import legado
    a = await asyncio.wait_for(asyncio.to_thread(_lk_chamar, legado.criar_auditoria, nome, data,
                                                 str(aud_cfg().get("comentario", "Aberta pelo robô do Telegram"))), 240)
    d = ler(ARQ_AUD, {})
    d["atual"] = a
    gravar(ARQ_AUD, d)
    return a


def _dm(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d/%m")


def _texto_aud_mes(sit: dict) -> str:
    feitas = ", ".join(f"{a['nome']} ({_dm(a['data'])})" for a in sit.get("mes", [])) or "nenhuma ainda"
    return f"Neste mês: {esc(feitas)}"


async def aud_oferecer(bot, chat, forcar: bool = False):
    """Pergunta no privado se abre a auditoria da semana (não pergunta se já existe uma)."""
    try:
        sit = await aud_situacao()
    except Exception as e:
        await enviar(bot, chat, f"⚠️ Não consegui consultar as auditorias no Leankeep.\n{esc(str(e)[:300])}")
        return
    if sit["atual"] and not forcar:
        return
    nome, dia = sit["proximo"], hoje()
    linhas = []
    if sit["atual"]:
        linhas.append(f"A auditoria desta semana já existe: <b>{esc(sit['atual']['nome'])}</b> ({_dm(sit['atual']['data'])}) – nº {sit['atual']['id']}.")
        linhas.append("As ocorrências que você registrar pelo robô entram nela.")
    linhas.append(_texto_aud_mes(sit))
    linhas.append(f"\nAbrir agora a <b>{esc(nome)}</b> com data de vistoria <b>{dia:%d/%m/%Y}</b>?")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🆕 Abrir {nome}", callback_data=f"au|c|{nome}|{dia:%Y%m%d}"),
                                InlineKeyboardButton("Agora não", callback_data="au|x")]])
    await enviar(bot, chat, "🔎 <b>BATIDÃO DA SEMANA (Auditoria Leankeep)</b>\n\n" + "\n".join(linhas), reply_markup=kb)


async def auditoria_semanal(context: ContextTypes.DEFAULT_TYPE):
    g = CFG.get("gestor_chat_id")
    if g and MODO == "leankeep":
        await aud_oferecer(context.bot, int(g))


async def cmd_auditoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/auditoria – mostra a auditoria da semana e oferece abrir a próxima."""
    if not _eh_gestor(update):
        await update.effective_message.reply_text("Só o gestor pode usar este comando.")
        return
    await update.effective_message.reply_text("⏳ Consultando as auditorias no Leankeep…")
    await aud_oferecer(context.bot, update.effective_chat.id, forcar=True)


async def aud_botao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _eh_gestor(update):
        await q.answer("Só o gestor abre auditorias.", show_alert=True)
        return
    p = q.data.split("|")
    if p[1] == "x":
        await q.answer("Ok.")
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
        return
    nome, dia = p[2], datetime.strptime(p[3], "%Y%m%d").date()
    if p[1] == "c":  # confirmar
        await q.answer()
        txt = (f"<b>Confira antes de criar:</b>\n• Unidade: CB JUNDIAÍ\n• Nome: <b>{esc(nome)}</b>\n"
               f"• Data da vistoria: <b>{dia:%d/%m/%Y}</b>\n• Responsável: Pedro Melo\n• Status: Pendente")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Criar {nome}", callback_data=f"au|s|{nome}|{p[3]}"),
                                    InlineKeyboardButton("✖️ Cancelar", callback_data="au|x")]])
        await q.edit_message_text(limpar_texto(txt), parse_mode=ParseMode.HTML, reply_markup=kb)
        return
    if p[1] == "s":
        await q.answer()
        await q.edit_message_text(f"⏳ Criando a {nome} no Leankeep… (até 1 minuto)")
        try:
            a = await aud_criar(nome, dia)
        except Exception as e:
            log.exception("Criar auditoria falhou")
            await enviar(context.bot, q.message.chat.id,
                         f"⚠️ <b>Não consegui criar a {esc(nome)}.</b>\n{esc(str(e)[:400])}\n\n"
                         f"<i>Confira na lista de Auditorias antes de tentar de novo.</i>",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                             "🔁 Tentar de novo", callback_data=f"au|s|{nome}|{p[3]}")]]))
            return
        extra = " (já existia, não criei outra)" if a.get("ja_existia") else ""
        await enviar(context.bot, q.message.chat.id,
                     f"✅ <b>{esc(a['nome'])} criada no Leankeep – nº {a['id']}</b>{extra}\n"
                     f"Data da vistoria: {_dm(a['data'])}\n\n"
                     f"As ocorrências que você registrar pelo robô nesta semana vão entrar nela.")


# =====================================================================
#  Seguimento no Leankeep (Correções › Incluir Correção): status, descrição e foto
# =====================================================================
ARQ_SEG = DADOS / "seguimento.json"


def seg_cfg() -> dict:
    return CFG.get("seguimento", {}) or {}


def legado_status(nome: str) -> str:
    import legado
    return next(c for c, n in legado.STATUS_CORRECAO.items() if n == nome)


def _sg_alvo(ident: str):
    """ident = nº do robô (31) ou nº do Leankeep (D171431). Devolve (numero_lk, item_do_robô, rótulo)."""
    ident = ident.strip().lstrip("#").upper()
    itens = oc_dados()["itens"]
    if ident.isdigit():
        o = itens.get(ident)
        lk = ((o or {}).get("leankeep") or {}).get("numero")
        return lk, o, f"#{ident}" + (f" ({lk})" if lk else "")
    for n, o in itens.items():
        if ((o.get("leankeep") or {}).get("numero") or "").upper() == ident:
            return ident, o, f"#{n} ({ident})"
    return ident, None, ident


def _sg_extra(ident: str, cod: str | None = None, **novo) -> dict:
    d = ler(ARQ_SEG, {})
    e = d.setdefault("itens", {}).setdefault(ident, {})
    if novo:
        e.update(novo)
        gravar(ARQ_SEG, d)
    return e


def _sg_foto(ident: str, o) -> tuple[str | None, str]:
    e = _sg_extra(ident)
    if e.get("foto_file"):
        return e["foto_file"], "a que você mandou aqui"
    sol = (o or {}).get("solucao") or {}
    if sol.get("foto_file"):
        return sol["foto_file"], "a da conclusão no grupo"
    return None, ""


async def sg_menu(bot, chat, ident, editar=None):
    import legado
    numero, o, rot = _sg_alvo(ident)
    if not numero:
        await enviar(bot, chat, f"A ocorrência {esc(rot)} ainda não foi registrada no Leankeep. "
                                f"Use /registrar_leankeep {esc(ident)} primeiro.")
        return
    atual = ((o or {}).get("leankeep") or {}).get("status") or "Gerado O.S"
    bts = [InlineKeyboardButton(("• " if n == atual else "") + n.replace("Encaminhada para ", "Enc. ")[:30],
                                callback_data=f"sg|c|{ident}|{c}")
           for c, n in legado.STATUS_CORRECAO.items() if c != "35788"]
    linhas = [bts[i:i + 2] for i in range(0, len(bts), 2)] + [[InlineKeyboardButton("✖️ Cancelar", callback_data="sg|x")]]
    txt = (f"🔧 <b>Seguimento da ocorrência {esc(rot)}</b>\n{esc(((o or {}).get('texto') or '')[:200])}\n\n"
           f"Status atual: <b>{esc(atual)}</b>\n<b>Qual o novo status?</b>")
    if editar:
        await editar.edit_message_text(limpar_texto(txt), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(linhas))
    else:
        await enviar(bot, chat, txt, reply_markup=InlineKeyboardMarkup(linhas))


async def sg_confirmar(bot, chat, ident, cod, editar=None):
    import legado
    numero, o, rot = _sg_alvo(ident)
    nome = legado.STATUS_CORRECAO.get(cod, "?")
    e = _sg_extra(ident)
    desc = e.get("desc") if e.get("desc_cod") == cod else None
    if not desc:
        desc = nome if cod != legado.SOLUCIONADA else (
            "Solucionada" + (f" – concluída por {(o.get('resolvida') or {}).get('por')}" if o and o.get("resolvida") else ""))
    foto, de_onde = _sg_foto(ident, o)
    precisa_foto = cod == legado.SOLUCIONADA
    L = [f"🔧 <b>Seguimento – {esc(rot)}</b>", esc(((o or {}).get("texto") or "")[:200]), "",
         f"• Novo status: <b>{esc(nome)}</b>", f"• Descrição: <i>{esc(desc[:300])}</i>",
         f"• Data/hora: {datetime.now(FUSO):%d/%m/%Y %H:%M}"]
    if foto:
        L.append(f"• Foto: sim ({de_onde})")
    elif precisa_foto:
        L.append("• Foto da resolução: <b>falta</b> (obrigatória para Solucionada)")
    L += ["", "<i>Para mudar a descrição, responda esta mensagem com o texto. "
              + ("Para mandar a foto, responda esta mensagem com a foto.</i>" if precisa_foto or foto
                 else "Se quiser anexar foto, responda com a foto.</i>")]
    linha1 = ([InlineKeyboardButton("✅ Gravar no Leankeep", callback_data=f"sg|s|{ident}|{cod}")]
              if (foto or not precisa_foto) else [])
    kb = InlineKeyboardMarkup([l for l in (linha1, [InlineKeyboardButton("↩️ Outro status", callback_data=f"sg|m|{ident}"),
                                                    InlineKeyboardButton("✖️ Cancelar", callback_data="sg|x")]) if l])
    if editar:
        await editar.edit_message_text(limpar_texto("\n".join(L)), parse_mode=ParseMode.HTML, reply_markup=kb)
        mid = editar.message.message_id
    else:
        r = await enviar(bot, chat, "\n".join(L), reply_markup=kb)
        mid = getattr(r, "message_id", None)
    if mid:
        d = ler(ARQ_SEG, {})
        pend = d.setdefault("pend", {})
        pend[str(mid)] = {"ident": ident, "cod": cod}
        for k in list(pend)[:-30]:  # guarda só as últimas
            pend.pop(k)
        gravar(ARQ_SEG, d)


async def sg_resposta_privada(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Resposta (texto ou foto) a uma tela de seguimento no privado. True se tratou."""
    msg = update.effective_message
    if not msg or not msg.reply_to_message or not _eh_gestor(update):
        return False
    p = (ler(ARQ_SEG, {}).get("pend") or {}).get(str(msg.reply_to_message.message_id))
    if not p:
        return False
    ident, cod = p["ident"], p["cod"]
    if msg.photo:
        _sg_extra(ident, foto_file=msg.photo[-1].file_id)
        if msg.caption:
            _sg_extra(ident, desc=msg.caption.strip(), desc_cod=cod)
    elif msg.text:
        _sg_extra(ident, desc=msg.text.strip(), desc_cod=cod)
    else:
        return False
    await sg_confirmar(context.bot, msg.chat.id, ident, cod)
    return True


async def sg_foto_privada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await sg_resposta_privada(update, context)


async def sg_botao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import legado
    q = update.callback_query
    if not _eh_gestor(update):
        await q.answer("Só o gestor dá seguimento no Leankeep.", show_alert=True)
        return
    p = q.data.split("|")
    if p[1] == "x":
        await q.answer("Ok.")
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
        return
    ident = p[2]
    await q.answer()
    if p[1] == "m":
        await sg_menu(context.bot, q.message.chat.id, ident, editar=None if q.message.photo else q)
        return
    cod = p[3]
    if p[1] == "c":
        await sg_confirmar(context.bot, q.message.chat.id, ident, cod, editar=None if q.message.photo else q)
        return
    if p[1] != "s":
        return
    numero, o, rot = _sg_alvo(ident)
    nome = legado.STATUS_CORRECAO.get(cod, "?")
    e = _sg_extra(ident)
    desc = e.get("desc") if e.get("desc_cod") == cod else None
    if not desc:
        desc = nome if cod != legado.SOLUCIONADA else (
            "Solucionada" + (f" – concluída por {(o.get('resolvida') or {}).get('por')}" if o and o.get("resolvida") else ""))
    desc += " (pelo Telegram)"
    foto_id, _ = _sg_foto(ident, o)
    try:
        await q.edit_message_text(f"⏳ Gravando o seguimento da {rot} no Leankeep… (até 1 minuto)")
    except Exception:
        pass
    try:
        foto = None
        if foto_id:
            f = await context.bot.get_file(foto_id)
            foto = bytes(await f.download_as_bytearray())
        res = await asyncio.wait_for(asyncio.to_thread(
            _lk_chamar, legado.seguimento, numero, cod, desc, foto, datetime.now(FUSO).replace(tzinfo=None)), 240)
    except Exception as ex:
        log.exception("Seguimento no Leankeep falhou")
        await enviar(context.bot, q.message.chat.id,
                     f"⚠️ <b>Não consegui gravar o seguimento da {esc(rot)}.</b>\n{esc(str(ex)[:400])}\n\n"
                     f"<i>Confira a ocorrência no Leankeep antes de tentar de novo.</i>",
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                         "🔁 Tentar de novo", callback_data=f"sg|c|{ident}|{cod}")]]))
        return
    _, _, aviso = res.partition("|")
    if o:
        dados = oc_dados()
        for n, it in dados["itens"].items():
            if it is not None and ((it.get("leankeep") or {}).get("numero") == numero):
                it["leankeep"]["status"] = nome
                it["leankeep"].setdefault("seguimentos", []).append(
                    {"status": nome, "em": datetime.now(FUSO).isoformat(timespec="minutes")})
        gravar(ARQ_OCORR, dados)
    d = ler(ARQ_SEG, {})
    d.get("itens", {}).pop(ident, None)
    gravar(ARQ_SEG, d)
    await enviar(context.bot, q.message.chat.id,
                 f"✅ <b>Seguimento gravado no Leankeep – {esc(rot)}</b>\nNovo status: <b>{esc(nome)}</b>"
                 + (" · com foto" if foto_id else "")
                 + (f"\n\n⚠️ <i>{esc(aviso)}</i>" if aviso else ""),
                 reply_markup=None if cod == legado.SOLUCIONADA else InlineKeyboardMarkup([[InlineKeyboardButton(
                     "🔧 Outro seguimento", callback_data=f"sg|m|{ident}")]]))


async def cmd_seguimento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/seguimento 31  ou  /seguimento D171431  (sem número: lista as que estão no Leankeep e não solucionadas)."""
    if not _eh_gestor(update):
        await update.effective_message.reply_text("Só o gestor pode usar este comando.")
        return
    if context.args:
        await sg_menu(context.bot, update.effective_chat.id, context.args[0])
        return
    abertas = sorted([o for o in oc_dados()["itens"].values()
                      if (o.get("leankeep") or {}).get("numero") and (o["leankeep"].get("status") != "Solucionada")],
                     key=lambda o: -o["num"])[:10]
    if not abertas:
        await update.effective_message.reply_text(
            "Nenhuma ocorrência registrada pelo robô está pendente no Leankeep.\n"
            "Para outra, use: /seguimento D171431")
        return
    bts = [[InlineKeyboardButton(f"#{o['num']} {o['leankeep']['numero']} · {o['leankeep'].get('status') or 'Gerado O.S'}"[:60],
                                 callback_data=f"sg|m|{o['num']}")] for o in abertas]
    await enviar(context.bot, update.effective_chat.id, "🔧 <b>Dar seguimento em qual ocorrência?</b>",
                 reply_markup=InlineKeyboardMarkup(bts))


async def cmd_registrar_leankeep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/registrar_leankeep 12 – manda de novo o cartão da ocorrência #12 com o botão de registrar."""
    if not _eh_gestor(update):
        await update.effective_message.reply_text("Só o gestor pode usar este comando.")
        return
    if not context.args:
        await update.effective_message.reply_text("Use assim: /registrar_leankeep 12")
        return
    await lk_oferecer(context.bot, context.args[0].lstrip("#"), chat=update.effective_chat.id)


def teclado_pergunta(chave_p: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📌 Abrir ocorrência", callback_data=f"ocq|s|{chave_p}"),
         InlineKeyboardButton("✅ Conclusão de ocorrência", callback_data=f"ocq|c|{chave_p}")],
        [InlineKeyboardButton("Não é ocorrência", callback_data=f"ocq|n|{chave_p}")]])


def teclado_conclusao(chave_p: str) -> InlineKeyboardMarkup:
    """Lista as ocorrências em aberto (mais recentes primeiro) para escolher qual foi concluída."""
    abertas = sorted([o for o in oc_dados()["itens"].values() if not o.get("resolvida")],
                     key=lambda o: -o["num"])[:int(oc_cfg().get("max_opcoes_conclusao", 8))]
    linhas = [[InlineKeyboardButton(f"#{o['num']} {(o.get('texto') or 'só foto')[:32]}",
                                    callback_data=f"ocq|f|{chave_p}|{o['num']}")] for o in abertas]
    linhas.append([InlineKeyboardButton("📌 É ocorrência nova", callback_data=f"ocq|s|{chave_p}"),
                   InlineKeyboardButton("Nenhuma", callback_data=f"ocq|n|{chave_p}")])
    return InlineKeyboardMarkup(linhas)


async def oc_botao_pergunta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resposta ao "do que se trata?" / "qual foi concluída?" (abrir, concluir ou não é nada)."""
    q = update.callback_query
    partes = q.data.split("|")
    resp, chave_p = partes[1], partes[2]
    dados = oc_dados()
    if resp == "c":  # conclusão: mostra a lista de ocorrências em aberto
        if chave_p not in dados.get("perguntas", {}):
            await q.answer("Essa pergunta já foi respondida.")
            return
        await q.answer()
        try:
            await q.edit_message_text("✅ Qual ocorrência foi concluída?", reply_markup=teclado_conclusao(chave_p))
        except Exception:
            pass
        return
    item = dados.get("perguntas", {}).pop(chave_p, None)
    if not item:
        await q.answer("Essa pergunta já foi respondida.")
        try:
            await q.message.delete()
        except Exception:
            pass
        return
    item.pop("msg_pergunta", None)
    gravar(ARQ_OCORR, dados)
    try:
        await q.message.delete()  # tira a pergunta do grupo para não poluir
    except Exception:
        pass
    if resp == "s":
        num = await oc_registrar(context.bot, dados, item)
        await q.answer(f"Ocorrência #{num} aberta")
    elif resp == "f":
        num = partes[3]
        o = oc_resolver(dados, num, q.from_user.full_name)
        if not o:
            await q.answer("Essa ocorrência já estava concluída.")
            return
        dados = oc_dados()
        dados["itens"][num]["solucao"] = {"chat_id": item["chat_id"], "msg_id": item["msg_ids"][0],
                                          "foto": item.get("foto"), "por": item.get("autor"),
                                          "foto_file": item.get("foto_file")}
        gravar(ARQ_OCORR, dados)
        from types import SimpleNamespace as _N
        await reagir(context.bot, _N(chat=_N(id=item["chat_id"]), message_id=item["msg_ids"][0]), "👍")
        await oc_concluida(context.bot, num)
        await q.answer(f"Ocorrência #{num} concluída!")
    else:
        await q.answer("Ok, não virou ocorrência.")


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
        await q.answer(f"#{num} concluída! Backup enviado ao gestor.")
        m = q.message
        texto_atual = m.text or m.caption or ""
        if texto_atual.startswith("📋"):  # lista antiga (tudo numa mensagem): só redesenha
            try:
                texto, teclado = oc_texto_lista()
                await q.edit_message_text(limpar_texto(texto), parse_mode=ParseMode.HTML,
                                          reply_markup=teclado, disable_web_page_preview=True)
            except Exception:
                pass
            await oc_concluida(context.bot, num)
        else:  # apaga esta mensagem e as demais do robô sobre a ocorrência; backup vai pro privado
            await oc_concluida(context.bot, num, extra=(m.chat.id, m.message_id))
        return
    # em andamento: atualiza a mensagem onde o botão foi tocado
    try:
        m = q.message
        texto_atual = m.text or m.caption or ""
        if texto_atual.startswith("#") or texto_atual.startswith("📌"):  # cartão ou registro
            if m.photo:
                await q.edit_message_caption(caption=limpar_texto(oc_cartao(o))[:1024], parse_mode=ParseMode.HTML,
                                             reply_markup=botao_oc(num, o))
            else:
                await q.edit_message_text(limpar_texto(oc_cartao(o)), parse_mode=ParseMode.HTML,
                                          reply_markup=botao_oc(num, o), disable_web_page_preview=True)
        elif texto_atual.startswith("📋"):  # lista antiga (tudo numa mensagem): redesenha inteira
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
                            and not (m.text or m.caption or "").startswith("📋"))
    if not no_registro_da_saida:
        r = await enviar(context.bot, sc or o["chat_id"], aviso, message_thread_id=st or None)
        if r is not None:
            oc_lembrar_msg(num, _Enviada(r.chat.id, r.message_id))


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


def oc_cartao(o: dict) -> str:
    """Uma ocorrência = uma mensagem, com os botões logo abaixo dela."""
    agora = datetime.now(FUSO)
    criada = datetime.fromisoformat(o["criada"])
    dias = (agora.date() - criada.date()).days
    idade = "hoje" if dias == 0 else ("há 1 dia" if dias == 1 else f"há {dias} dias")
    texto = esc(o["texto"][:300] + ("…" if len(o["texto"]) > 300 else "")) or "(só foto)"
    link = f"<a href=\"{oc_link(o)}\">{'↗ ver no grupo' if o.get('foto') else '↗ ver mensagem'}</a>"
    L = [f"<b>#{o['num']}</b> {oc_situacao(o)}" + ("" if o.get("resolvida") else f" · {idade}"),
         texto,
         f"<i>{esc(o['autor'])} · {criada.strftime('%d/%m %H:%M')}</i> · {link}"]
    if o.get("resolvida"):
        r = o["resolvida"]
        L.append(f"<i>Concluída por {esc(r['por'])} em {datetime.fromisoformat(r['em']):%d/%m %H:%M}</i>")
    elif o.get("andamento"):
        L.append(f"<i>Em andamento com {esc(o['andamento']['por'])}</i>")
    return "\n".join(L)


def oc_cabecalho_lista() -> str:
    texto, _ = oc_texto_lista()
    # só o topo da lista (título, totais e ontem); as ocorrências vão uma por mensagem
    topo = texto.split("\n\n<b>#", 1)[0]
    return topo.replace("\n\n<i>Use os botões abaixo para atualizar cada ocorrência.</i>", "")


async def oc_enviar_lista(bot, chat, thread=None):
    if not chat:
        return
    dados = oc_dados()
    abertas = sorted([o for o in dados["itens"].values() if not o.get("resolvida")], key=lambda o: o["num"])
    cab = oc_cabecalho_lista()
    if abertas:
        cab += "\n\n<i>Cada ocorrência vem a seguir, com os botões logo abaixo.</i>"
    await enviar(bot, chat, cab, message_thread_id=thread)
    for o in abertas[:40]:
        r = await oc_enviar_com_foto(bot, chat, thread, o, oc_cartao(o), botao_oc(o["num"], o))
        oc_lembrar_msg(o["num"], r)
    if len(abertas) > 40:
        await enviar(bot, chat, f"… e mais {len(abertas) - 40} ocorrências. Use /pendencias para ver.",
                     message_thread_id=thread)


async def oc_publicar_lista(context: ContextTypes.DEFAULT_TYPE):
    c, t = oc_saida()
    if c:
        await oc_enviar_lista(context.bot, c, t or None)
    if CFG.get("gestor_chat_id"):
        await oc_enviar_lista(context.bot, CFG["gestor_chat_id"])


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
    msg = update.message
    await oc_enviar_lista(context.bot, update.effective_chat.id,
                          msg.message_thread_id if msg.is_topic_message else None)


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
        await oc_concluida(context.bot, n)
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


def cobrancas_citadas(texto: str):
    """Cobranças de hoje que a mensagem cita pelo assunto ("consumo de água, gás e energia" →
    Leituras). Usa os 'apelidos' de cada cobrança no config. Fica com as que mais batem."""
    t = " " + " ".join(re.sub(r"[^A-Z0-9 ]", " ", normalizar(texto)).split())
    pontos = []
    for cob in CFG.get("cobrancas", []):
        if cob.get("so_gestor"):
            continue
        dia = data_ref(cob)
        if not roda_hoje(cob, dia):
            continue
        n = sum(1 for a in cob.get("apelidos", [])
                if " " + " ".join(re.sub(r"[^A-Z0-9 ]", " ", normalizar(a)).split()) in t)
        if n:
            pontos.append((n, cob))
    if not pontos:
        return []
    melhor = max(n for n, _ in pontos)
    return [c for n, c in pontos if n == melhor]


def cobrancas_em_andamento():
    """Cobranças cujo 1º aviso já passou (no dia a que se referem)."""
    agora = datetime.now(FUSO)
    saida = []
    for cob in CFG.get("cobrancas", []):
        if cob.get("so_gestor"):
            continue  # cobrança só do privado: não entra nas conferências do grupo
        dia = data_ref(cob)
        if not roda_hoje(cob, dia):
            continue
        inicio = datetime.combine(dia, hora(cob["aviso"]))
        if inicio <= agora:
            saida.append(cob)
    return saida


# ---------------- baixa declarada pela equipe (o site do Leankeep demora a atualizar) ----------------
ARQ_DECL = DADOS / "baixas_declaradas.json"


def dcfg() -> dict:
    return CFG.get("baixa_declarada", {}) or {}


def _chave_decl(cob) -> str:
    return f"{data_ref(cob).isoformat()}|{CFG['cobrancas'].index(cob)}"


def declarada(cob):
    """Alguém já disse que baixou esta cobrança hoje? (então o robô não cobra mais no grupo)"""
    if not dcfg().get("ativo", True):
        return None
    return ler(ARQ_DECL, {}).get(_chave_decl(cob))


def declarar(cob, quem: str, via: str):
    dados = ler(ARQ_DECL, {})
    k = _chave_decl(cob)
    if k not in dados:
        dados[k] = {"cobranca": cob["nome"], "quem": quem, "via": via,
                    "em": datetime.now(FUSO).isoformat(timespec="minutes"), "situacao": "aguardando"}
        limite = (hoje() - timedelta(days=5)).isoformat()
        dados = {kk: v for kk, v in dados.items() if kk.split("|")[0] >= limite}
        gravar(ARQ_DECL, dados)
        log.info("Baixa declarada: %s por %s (%s)", cob["nome"], quem, via)


def cobranca_da_foto():
    """Foto solta no tópico: vale para a cobrança em andamento agora (entre o 1º aviso e o
    último reforço). Se houver mais de uma, vale para a que o robô cobrou por último."""
    agora = datetime.now(FUSO)
    ref = CFG.get("reforco", {})
    ativas = []
    for i, cob in enumerate(CFG.get("cobrancas", [])):
        if cob.get("so_gestor"):
            continue
        dia = data_ref(cob)
        if not roda_hoje(cob, dia):
            continue
        ini = datetime.combine(dia, hora(cob["aviso"]))
        intervalo = int(cob.get("reforco_intervalo_min", ref.get("intervalo_min", 30)))
        maximo = int(cob.get("reforco_maximo", ref.get("maximo", 3)))
        fim = prazo_dt(cob, dia) + timedelta(minutes=intervalo * maximo + 30)
        if ini <= agora <= fim:
            ativas.append(i)
    if len(ativas) > 1:
        ultimas = {i: max([m for m, c in MSG_COBRANCA.items() if c == i], default=0) for i in ativas}
        if max(ultimas.values()) > 0:
            ativas = [max(ultimas, key=ultimas.get)]
        else:  # sem registro: a que começou mais recentemente
            ativas = [max(ativas, key=lambda i: datetime.combine(data_ref(CFG["cobrancas"][i]),
                                                                  hora(CFG["cobrancas"][i]["aviso"])))]
    return [CFG["cobrancas"][i] for i in ativas]


async def reagir(bot, msg, emoji="👍"):
    try:
        await bot.set_message_reaction(msg.chat.id, msg.message_id, emoji)
    except Exception as e:
        log.info("Não consegui reagir à mensagem: %s", e)


async def verificar_declaradas(context):
    """Confere em silêncio as baixas declaradas. Se depois do tempo-limite o Leankeep
    ainda mostrar pendência, avisa só o gestor, no privado (nunca cobra no grupo)."""
    if MODO != "leankeep":
        return
    dados = ler(ARQ_DECL, {})
    if not dados:
        return
    agora = datetime.now(FUSO)
    espera = timedelta(minutes=int(dcfg().get("conferir_apos_min", 60)))
    limite = timedelta(minutes=int(dcfg().get("avisar_gestor_apos_min", 240)))
    mudou = False
    for k, d in dados.items():
        if d.get("situacao") != "aguardando":
            continue
        em = datetime.fromisoformat(d["em"])
        if agora - em < espera:
            continue
        try:
            cob = CFG["cobrancas"][int(k.split("|")[1])]
            dia = date.fromisoformat(k.split("|")[0])
            pend = sem_duplicadas(await buscar_pendentes(cob.get("palavras"), dia, cob.get("excluir")))
        except Exception as e:
            log.warning("Conferência de baixa declarada falhou: %s", e)
            continue
        if not pend:
            d["situacao"] = "confirmada"
            d["confirmada_em"] = agora.isoformat(timespec="minutes")
            mudou = True
            log.info("Baixa declarada confirmada no Leankeep: %s", d["cobranca"])
        elif agora - em >= limite:
            d["situacao"] = "nao_confirmada"
            mudou = True
            L = [f"⚠️ <b>Baixa declarada que o Leankeep ainda não mostra</b>", "",
                 f"<b>{esc(d['cobranca'])}</b> – dia {dia:%d/%m}",
                 f"• Declarada por {esc(d['quem'])} às {em:%H:%M} ({esc(d['via'])})",
                 f"• Depois de {int(limite.total_seconds() // 3600)}h, o Leankeep ainda mostra {len(pend)} pendente(s):"]
            L += [f"• {esc(legivel(a.descricao))}" for a in pend[:15]]
            L += ["", "<i>Pode ser só atraso do site ou baixa que não foi salva no app. "
                      "O grupo NÃO foi cobrado.</i>"]
            await enviar(context.bot, CFG.get("gestor_chat_id"), "\n".join(L))
    if mudou:
        gravar(ARQ_DECL, dados)


async def cmd_conferir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/conferir – mostra, para cada cobrança de hoje, o que o robô está vendo como pendente."""
    if not _eh_gestor(update):
        await update.message.reply_text("Só o gestor pode usar /conferir.")
        return
    if MODO != "leankeep":
        await update.message.reply_text("O robô está sem acesso ao Leankeep.")
        return
    L = [f"🔎 <b>O que o robô vê no Leankeep agora</b> ({datetime.now(FUSO):%d/%m %H:%M})"]
    for cob in CFG.get("cobrancas", []):
        dia = data_ref(cob)
        if not roda_hoje(cob, dia):
            continue
        try:
            pend = sem_duplicadas(await buscar_pendentes(cob.get("palavras"), dia, cob.get("excluir")))
        except Exception as e:
            L += ["", f"<b>{esc(cob['nome'])}</b>: erro ao consultar ({esc(e)})"]
            continue
        dec = declarada(cob)
        L += ["", f"<b>{esc(cob['nome'])}</b> – {'tudo baixado' if not pend else f'{len(pend)} pendente(s)'}"
              + (f" · baixa declarada por {esc(dec['quem'])} às {dec['em'][11:16]}" if dec else "")]
        for a in pend[:15]:
            L.append(f"• {esc(legivel(a.descricao))} <i>(nº {esc(a.numero or '?')}, situação: {esc(a.status)})</i>")
    L += ["", "<i>Compare com o site: se algo aqui já está baixado lá, me mande o nº da atividade.</i>"]
    await enviar(context.bot, update.effective_chat.id, "\n".join(L))


async def conferir_baixa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    texto = (msg.text or msg.caption or "").strip()
    if MODO != "leankeep" or not eh_aviso_de_baixa(texto):
        return
    import time as _t
    if _t.time() - ULTIMA_CONFERENCIA.get(msg.chat.id, 0) < 30:  # evita consultas repetidas
        return
    ULTIMA_CONFERENCIA[msg.chat.id] = _t.time()

    alvo = msg.reply_to_message
    if alvo and alvo.message_id in MSG_COBRANCA:
        cobs = [CFG["cobrancas"][MSG_COBRANCA[alvo.message_id]]]
    else:  # se a mensagem diz o que foi baixado, vale isso; senão, a cobrança em andamento
        cobs = cobrancas_citadas(texto) or cobrancas_em_andamento()
    if not cobs:
        return
    quem = msg.from_user.first_name if msg.from_user else ""
    faltando = []
    for cob in cobs:
        try:
            ativs = sem_duplicadas(await buscar_pendentes(cob.get("palavras"), data_ref(cob),
                                                          cob.get("excluir")))
        except Exception as e:
            await msg.reply_text(f"Não consegui conferir no Leankeep agora: {e}")
            return
        if ativs:
            faltando.append((cob, ativs))
    if not faltando:
        await msg.reply_text(texto_tudo_baixado(cobs, quem))
        return
    if dcfg().get("ativo", True):  # confia na equipe agora e confere depois, em silêncio
        for cob, _ in faltando:
            declarar(cob, msg.from_user.full_name if msg.from_user else quem, "escreveu no grupo")
        await reagir(context.bot, msg)
        return
    await enviar(context.bot, msg.chat.id, texto_faltam_baixas(quem, faltando),
                 message_thread_id=msg.message_thread_id if msg.is_topic_message else None,
                 reply_to_message_id=msg.message_id)


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
    try:
        if await BAR.on_mensagem(update, context):
            return  # mensagem do grupo do bar
    except Exception:
        log.exception("Erro nas rotinas do bar")
    if no_topico_ocorrencias(msg) or no_topico_saida(msg):
        await oc_mensagem(update, context)
    elif no_topico_leankeep(msg):
        if not await receber_msg_livre(update, context):
            if msg.photo and MODO == "leankeep" and dcfg().get("foto_conta", True):
                alvo = msg.reply_to_message
                cobs = ([CFG["cobrancas"][MSG_COBRANCA[alvo.message_id]]]
                        if alvo and alvo.message_id in MSG_COBRANCA
                        else cobrancas_citadas(msg.caption or "") or cobranca_da_foto())
                cobs = [c for c in cobs if not declarada(c)]
                if cobs:
                    for c in cobs:
                        declarar(c, msg.from_user.full_name if msg.from_user else "?", "postou foto")
                    await reagir(context.bot, msg)
                    return
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
        await q.answer("Conferindo no Leankeep…")
        try:
            pend = sem_duplicadas(await buscar_pendentes(cob.get("palavras"), data_ref(cob), cob.get("excluir")))
        except Exception as e:
            await enviar(context.bot, chat, f"Não consegui conferir no Leankeep agora: {esc(e)}",
                         message_thread_id=top, reply_to_message_id=q.message.message_id)
            return
        registrar_resposta(cob, quem, "realizada" if not pend else "realizada_mas_pendente")
        if pend and dcfg().get("ativo", True):
            declarar(cob, quem, "botão Baixa realizada")
            await enviar(context.bot, chat,
                         f"✅ <b>{esc(cob['nome'])}</b>\n\n{_mencao_usuario(u)} confirmou a baixa. Valeu!\n"
                         f"<i>O site do Leankeep demora a atualizar: vou conferir mais tarde, sem cobrar o grupo.</i>",
                         message_thread_id=top, reply_to_message_id=q.message.message_id)
            return
        if not pend:
            await enviar(context.bot, chat,
                         f"✅ <b>{esc(cob['nome'])}</b>\n\n{_mencao_usuario(u)} confirmou a baixa.\n"
                         f"Conferi no Leankeep: <b>tudo baixado!</b> Valeu!",
                         message_thread_id=top, reply_to_message_id=q.message.message_id)
            return
        L = [f"⚠️ <b>{esc(cob['nome'])}</b>", "",
             f"{_mencao_usuario(u)} marcou <b>Baixa realizada</b>, mas no Leankeep ainda falta ({len(pend)}):"]
        L += [f"• {esc(legivel(a.descricao))}" for a in pend[:20]]
        if len(pend) > 20:
            L.append(f"• … e mais {len(pend) - 20}")
        L += ["", "<i>Confira se a baixa foi salva no app (às vezes fica sem sincronizar).</i>"]
        await enviar(context.bot, chat, "\n".join(L), message_thread_id=top,
                     reply_to_message_id=q.message.message_id)
        if g and CFG.get("avisar_respostas_gestor", True):
            await enviar(context.bot, int(g), f"⚠️ <b>{esc(quem)}</b> marcou Baixa realizada em "
                                              f"<b>{esc(cob['nome'])}</b>, mas o Leankeep ainda mostra "
                                              f"{len(pend)} pendente(s).")
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
    g = _hospede_tagme(o)
    if g and g.get("name"):
        return str(g["name"]).strip().split(" ")[0].title()
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
        if "T" in v:
            # A Tagme grava o horário LOCAL com "Z" no fim (ex.: 19:30 vem como "T19:30:00.000Z").
            # Converter para o fuso tirava 3 horas (19:30 aparecia como 16:30), então vale o relógio escrito.
            if tg_cfg().get("horario_em_utc", False):
                d = _dt(v)
            else:
                m = re.search(r"T(\d{2}):(\d{2})", v)
                d = datetime(2000, 1, 1, int(m.group(1)), int(m.group(2))) if m else None
            if d and (d.hour, d.minute) != (0, 0):
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


_NOME_RUIM = ("username", "filename", "typename", "statusname", "venuename", "tablename", "areaname",
              "sectionname", "companyname", "partnername", "sourcename", "channelname", "eventname",
              "occasionname", "tagname", "createdbyname", "updatedbyname", "employeename", "staffname")


def _nome_profundo(o, caminho="", prof=0, achados=None):
    """Último recurso: varre a reserva inteira atrás de qualquer campo de nome de pessoa."""
    if achados is None:
        achados = []
    if prof > 4:
        return achados
    if isinstance(o, dict):
        for k, v in o.items():
            kl = k.lower().replace("_", "").replace("-", "")
            cam = f"{caminho}.{kl}"
            if any(t in cam for t in _NAO_E_PESSOA):
                continue
            if isinstance(v, str) and v.strip() and ("name" in kl or "nome" in kl) and kl not in _NOME_RUIM \
                    and not v.strip().startswith(("http", "{")) and not any(ch.isdigit() for ch in v[:3]):
                achados.append((prof, 0 if kl in ("fullname", "name", "nome", "displayname") else 1, v))
            elif isinstance(v, (dict, list)):
                _nome_profundo(v, cam, prof + 1, achados)
    elif isinstance(o, list):
        for v in o[:3]:
            _nome_profundo(v, caminho, prof + 1, achados)
    return achados


def _hospede_tagme(o: dict) -> dict | None:
    """Na Tagme o cliente vem em venueUser.guest (name + lastName)."""
    vu = o.get("venueUser") if isinstance(o, dict) else None
    if isinstance(vu, dict):
        g = vu.get("guest")
        if isinstance(g, dict):
            return g
        if vu.get("searchName"):
            return {"name": vu.get("searchName"), "lastName": vu.get("searchLastName")}
    return None


def _nome_completo(o: dict) -> str:
    g = _hospede_tagme(o)
    if g and (g.get("name") or g.get("lastName")):
        v = " ".join(str(x).strip() for x in (g.get("name"), g.get("lastName")) if x and str(x).strip())
        if v:
            return " ".join(v.split()).title()
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
    if not v:
        ach = _nome_profundo(o)
        if ach:
            ach.sort(key=lambda x: (x[0], x[1]))
            # nome + sobrenome separados (firstName / lastName)
            prim = next((x[2] for x in ach if x[1] == 1), "")
            v = ach[0][2] if ach[0][1] == 0 else prim
    if not v:
        _logar_sem_nome(o)
    v = " ".join(str(v).split()).strip(" -–—.,") if v else ""
    return v.title() if v else "(sem nome)"


_SEM_NOME_LOGADO = []


def _logar_sem_nome(o):
    """Registra no log (uma vez) a ESTRUTURA da reserva sem nome — só os nomes dos campos, sem dados."""
    if _SEM_NOME_LOGADO:
        return
    _SEM_NOME_LOGADO.append(1)

    def campos(d, prof=0):
        if isinstance(d, dict):
            return {k: campos(v, prof + 1) if prof < 4 else type(v).__name__ for k, v in d.items()}
        if isinstance(d, list):
            return [campos(d[0], prof + 1)] if d and prof < 4 else "lista"
        return type(d).__name__
    log.warning("TAGME SEM NOME – campos da reserva: %s", json.dumps(campos(o), ensure_ascii=False)[:3000])


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


def botoes_tagme(modo: str = "amanha") -> InlineKeyboardMarkup:
    if modo == "hoje":
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Atualizar hoje", callback_data="tg|hoje"),
                                      InlineKeyboardButton("🕒 Fila agora", callback_data="tg|agora")]])
    if modo == "amanha":
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Atualizar amanhã", callback_data="tg|amanha")]])
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
        dia = (h + timedelta(days=1) if modo == "amanha" else h if modo == "hoje"
               else date.fromisoformat(modo))
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
    # "hoje_e_amanha": duas mensagens — reservas de hoje e, separada, as de amanhã
    for m in (["hoje", "amanha"] if modo == "hoje_e_amanha" else [modo]):
        try:
            texto = await montar_tagme(m)
        except Exception as e:
            await tg_erro(context.bot, e)
            if chat_destino:
                await enviar(context.bot, chat_destino, f"Erro ao consultar a Tagme: {esc(e)}",
                             message_thread_id=thread)
            return
        kb = botoes_tagme(m if m in ("hoje", "amanha") else "")
        if chat_destino:
            await enviar(context.bot, chat_destino, texto, message_thread_id=thread, reply_markup=kb)
        else:
            await tg_enviar(context.bot, texto, reply_markup=kb)


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
    kb = botoes_tagme(modo if modo in ("hoje", "amanha") else "")
    try:
        await q.edit_message_text(limpar_texto(texto), parse_mode=ParseMode.HTML, reply_markup=kb,
                                  disable_web_page_preview=True)
    except Exception as e:  # mensagem igual ou longa demais: manda nova
        if "not modified" not in str(e).lower():
            await enviar(context.bot, q.message.chat.id, texto, reply_markup=kb,
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
    """/reservas (hoje e amanhã) · /reservas hoje|amanha|ontem|agora|25/12"""
    if not TG:
        await update.message.reply_text("A Tagme ainda não está configurada (TAGME_EMAIL/TAGME_SENHA no .env).")
        return
    modo = "hoje_e_amanha"
    if context.args:
        a = normalizar(context.args[0]).lower()
        h = hoje()
        if a.startswith("amanh"):
            modo = "amanha"
        elif a.startswith("hoje"):
            modo = "hoje"
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



async def cmd_configurar_cardapio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mandado DENTRO do tópico "Cardápio Tagme": a conferência diária do cardápio passa a sair ali."""
    msg = update.message
    if update.effective_chat.type == "private":
        await msg.reply_text("Envie este comando DENTRO do tópico \"Cardápio Tagme\" do grupo Bot Diretoria.")
        return
    if CFG.get("gestor_chat_id") and update.effective_user.id != int(CFG["gestor_chat_id"]):
        await msg.reply_text("Só o gestor pode configurar.")
        return
    topico = msg.message_thread_id if msg.is_topic_message else 0
    salvar_ajuste(card_grupo_chat_id=update.effective_chat.id, card_topico_id=topico)
    await msg.reply_text("✅ Pronto! A conferência diária do cardápio da Tagme (o que saiu, entrou e mudou de preço) "
                         "vai chegar aqui, todo dia às " + card_horarios_txt() + ".")

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
        "reservas": ("📅 Reservas da Tagme (hoje e amanhã)", resumo_tagme, "tópico Bot da Tagme"),
        "relogio": ("⏰ Lembrete dos relógios", lembrete_relogio, "grupo de fechamento"),
        "semanal": ("📈 Resumo semanal", enviar_semanal, "seu privado"),
        "cardapio": ("🍽️ Cardápio da Tagme: última conferência", cardapio_reenviar, "tópico Cardápio Tagme"),
    }
    for i, cob in enumerate(CFG.get("cobrancas", [])):
        r[f"cob{i}"] = (f"🔔 Cobrança: {cob['nome']}", None, "tópico Leankeep diário")
    for i, lem in enumerate(CFG.get("lembretes", [])):
        r[f"lem{i}"] = (f"🚨 Lembrete: {lem.get('texto', '')[:40]}", None, f"onde: {lem.get('onde', 'leankeep')}")
    return r


ARQ_LK_AREAS = DADOS / "leankeep_areas.json"
ARQ_CARDAPIO = DADOS / "cardapio.json"


def card_cfg() -> dict:
    return CFG.get("cardapio", {}) or {}


def card_horarios_txt() -> str:
    h = card_cfg().get("horarios") or [card_cfg().get("horario", "09:00")]
    return " e ".join(h)


async def cartao_cardapio(bot, chat, item: dict, tipo: str, topico=None):
    """Um item do cardápio: foto do produto + código, descrição e preço, com botões."""
    import cardapio as CD
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Abrir no GIA", url=CD.LINK_GIA),
                                InlineKeyboardButton("📖 Ver no cardápio", url=CD.LINK_CARDAPIO)]])
    texto = CD.legenda(item, tipo, esc)
    if item.get("foto"):
        try:
            await bot.send_photo(chat, item["foto"], caption=limpar_texto(texto)[:1024], parse_mode=ParseMode.HTML,
                                 reply_markup=kb, message_thread_id=topico)
            return
        except Exception as e:
            log.info("Cardápio: não consegui mandar a foto de %s: %s", item.get("codigo"), e)
    await enviar(bot, chat, texto, reply_markup=kb, message_thread_id=topico)


def texto_mudancas_cardapio(dif: dict, total_pratos: int, desde: str) -> str:
    """Mensagem didática, no formato do quadro de vinhos: um bloco por tipo de mudança."""
    import cardapio as CD
    L = [f"🍽️ <b>Cardápio da Tagme – o que mudou</b> ({datetime.now(FUSO):%d/%m})",
         f"<i>Comparando com a conferência de {esc(desde)}</i>"]
    blocos = (("saiu", "SAIU DO CARDÁPIO", "🔴", "O cliente não vê mais este item."),
              ("entrou", "ENTROU NO CARDÁPIO", "🟢", "Agora aparece para o cliente."),
              ("preco", "PREÇO MUDOU", "🟣", ""))
    for chave, titulo, cor, explica in blocos:
        itens = dif[chave]
        L += ["", f"<b>{titulo}</b> {cor} ({len(itens)})"]
        if not itens:
            L.append("<i>Nada.</i>")
            continue
        for i in itens[:25]:
            L.append(f"• <b>{esc(CD.nome_item(i))}</b> · cód. <code>{esc(i.get('codigo') or 'sem código')}</code>")
            if chave == "preco":
                L.append(f"   {CD.reais(i.get('preco_antes'))} → <b>{CD.reais(i.get('preco'))}</b>")
            else:
                sec = (i.get("categoria") or "").split(" > ")[-1]
                L.append(f"   {esc(sec)} · {CD.reais(i.get('preco'))}. {explica}")
        if len(itens) > 25:
            L.append(f"   … e mais {len(itens) - 25}")
    L += ["", f"<i>Hoje o cardápio tem {total_pratos} pratos e bebidas.</i>",
          "<i>Para ajustar no GIA: abra, escolha a loja 50.CB JUNDIAÍ e busque pelo código.</i>"]
    return "\n".join(L)



def _enviar(*a, **kw):
    return enviar(*a, **kw)

async def cardapio_diario(context: ContextTypes.DEFAULT_TYPE, chat=None, topico_chat=None):
    """Compara o cardápio da Tagme de hoje com o da última conferência e explica o que mudou (privado)."""
    import cardapio as CD
    topico = None
    if chat:
        destino, topico = chat, topico_chat
    elif CFG.get("card_grupo_chat_id"):  # tópico "Cardápio Tagme" (configurado com /configurar_cardapio)
        destino, topico = CFG["card_grupo_chat_id"], CFG.get("card_topico_id") or None
    else:
        destino = CFG.get("gestor_chat_id")
    if not destino:
        return

    async def enviar(bot, chat_id, texto, **kw):  # tudo desta conferência vai para o mesmo tópico
        if topico and chat_id == destino:
            kw.setdefault("message_thread_id", topico)
        return await _enviar(bot, chat_id, texto, **kw)
    try:
        agora = await asyncio.wait_for(asyncio.to_thread(CD.buscar, int(card_cfg().get("store_id", 1217))), 120)
    except Exception as e:
        log.warning("Cardápio: erro ao ler a Tagme: %s", e)
        # erro técnico: só no privado do gestor (não polui o grupo)
        await _enviar(context.bot, chat or CFG.get("gestor_chat_id"),
                      f"⚠️ Não consegui abrir o cardápio da Tagme agora. Tento de novo depois.\n"
                      f"<i>Detalhe: {esc(str(e)[:150])}</i>")
        return
    pratos = len({i["produto_id"] for i in agora.values()})
    ant = ler(ARQ_CARDAPIO, {})
    gravar(ARQ_CARDAPIO, {"em": datetime.now(FUSO).isoformat(timespec="minutes"), "itens": agora,
                          "ultima_dif": ant.get("ultima_dif")})
    if not ant.get("itens"):
        await enviar(context.bot, destino,
                     f"📖 <b>Cardápio da Tagme – CB Jundiaí</b>\n\n"
                     f"Pronto, já conferi o cardápio de hoje: <b>{pratos} pratos e bebidas</b>.\n\n"
                     f"A partir de agora, todo dia às {esc(card_horarios_txt())} eu te conto o que mudou:\n"
                     f"• o que <b>saiu</b> do cardápio 🔴\n• o que <b>entrou</b> no cardápio 🟢\n"
                     f"• o que <b>mudou de preço</b> 🟣\n\n"
                     f"Se nada mudar, não mando mensagem.\n\n"
                     f"Quer ver um prato com foto? Escreva /cardapio_item e o código ou o nome.\n"
                     f"Exemplo: <code>/cardapio_item bacalhau</code>")
        return
    dif = CD.comparar(ant["itens"], agora)
    n = sum(len(v) for v in dif.values())
    desde = ant.get("em", "")[:16].replace("T", " às ")
    desde = f"{desde[8:10]}/{desde[5:7]}{desde[10:]}" if len(desde) >= 10 else desde
    if not n:
        if chat:
            await enviar(context.bot, destino, f"🍽️ Cardápio da Tagme: <b>nada mudou</b> desde a conferência de {esc(desde)}.\n"
                                               f"<i>{pratos} pratos e bebidas no cardápio.</i>",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                             "🔄 Atualizar agora", callback_data="cd|agora")]]))
        return
    d = ler(ARQ_CARDAPIO, {})
    d["ultima_dif"] = dif
    d["ultima_dif_info"] = {"desde": desde, "pratos": pratos, "em": datetime.now(FUSO).isoformat(timespec="minutes")}
    gravar(ARQ_CARDAPIO, d)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📷 Ver com fotos", callback_data="cd|fotos"),
                                InlineKeyboardButton("🔗 Abrir no GIA", url=CD.LINK_GIA)],
                               [InlineKeyboardButton("🔄 Atualizar agora", callback_data="cd|agora")]])
    await enviar(context.bot, destino, texto_mudancas_cardapio(dif, pratos, desde), reply_markup=kb)



async def cardapio_reenviar(context: ContextTypes.DEFAULT_TYPE, chat=None):
    """Manda de novo a última mensagem de mudanças do cardápio (mesmo destino da conferência)."""
    import cardapio as CD
    d = ler(ARQ_CARDAPIO, {}) or {}
    dif, info = d.get("ultima_dif") or {}, d.get("ultima_dif_info") or {}
    if chat:
        destino, topico = chat, None
    elif CFG.get("card_grupo_chat_id"):
        destino, topico = CFG["card_grupo_chat_id"], CFG.get("card_topico_id") or None
    else:
        destino, topico = CFG.get("gestor_chat_id"), None
    if not any(dif.values()):
        await enviar(context.bot, destino, "🍽️ Cardápio da Tagme: não há mudança registrada para reenviar.",
                     message_thread_id=topico)
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📷 Ver com fotos", callback_data="cd|fotos"),
                                InlineKeyboardButton("🔗 Abrir no GIA", url=CD.LINK_GIA)],
                               [InlineKeyboardButton("🔄 Atualizar agora", callback_data="cd|agora")]])
    texto = texto_mudancas_cardapio(dif, info.get("pratos", 0), info.get("desde", "?"))
    texto = texto.replace("conferência de ?", "conferência anterior")
    if info.get("em"):
        em = datetime.fromisoformat(info["em"])
        texto = texto.replace(f"({datetime.now(FUSO):%d/%m})", f"({em:%d/%m às %H:%M})", 1)
    await enviar(context.bot, destino, texto, reply_markup=kb, message_thread_id=topico)

async def botao_cardapio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📷 Ver com fotos: manda um cartão (foto, código, descrição, preço) para cada mudança."""
    q = update.callback_query
    if q.data == "cd|agora":  # 🔄 Atualizar agora: confere o cardápio neste momento, no mesmo chat/tópico
        await q.answer("Conferindo o cardápio agora…")
        await cardapio_diario(context, chat=q.message.chat.id,
                              topico_chat=q.message.message_thread_id if q.message.is_topic_message else None)
        return
    dif = (ler(ARQ_CARDAPIO, {}) or {}).get("ultima_dif") or {}
    if not any(dif.values()):
        await q.answer("Não há mudanças para mostrar.")
        return
    await q.answer("Mandando as fotos…")
    limite = int(card_cfg().get("max_cartoes", 15))
    n = 0
    for tipo in ("saiu", "entrou", "preco"):
        for item in dif.get(tipo, []):
            if n >= limite:
                break
            await cartao_cardapio(context.bot, q.message.chat.id, item, tipo,
                                  q.message.message_thread_id if q.message.is_topic_message else None)
            n += 1


async def cmd_cardapio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cardapio – compara agora o cardápio da Tagme com a última leitura."""
    if not _eh_gestor(update):
        await update.effective_message.reply_text("Só o gestor pode usar /cardapio.")
        return
    await cardapio_diario(context, chat=update.effective_chat.id)


async def cmd_cardapio_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cardapio_item 5099  ou  /cardapio_item bacalhau – mostra o(s) item(ns) do cardápio com foto."""
    if not _eh_gestor(update):
        await update.effective_message.reply_text("Só o gestor pode usar /cardapio_item.")
        return
    termo = " ".join(context.args or []).strip()
    if not termo:
        await update.effective_message.reply_text("Use assim: /cardapio_item 5099  ou  /cardapio_item bacalhau")
        return
    import cardapio as CD
    itens = (ler(ARQ_CARDAPIO, {}) or {}).get("itens") or {}
    if not itens:
        try:
            itens = await asyncio.wait_for(asyncio.to_thread(CD.buscar, int(card_cfg().get("store_id", 1217))), 120)
        except Exception as e:
            await enviar(context.bot, update.effective_chat.id, f"Erro ao ler o cardápio: {esc(e)}")
            return
    t = normalizar(termo)
    achados = [i for i in itens.values()
               if t in normalizar(i.get("codigo", "")).split("-") or t == normalizar(i.get("codigo", ""))
               or t in normalizar(CD.nome_item(i))]
    if not achados:
        await enviar(context.bot, update.effective_chat.id,
                     f"Não achei <b>{esc(termo)}</b> no cardápio da Tagme (pode estar fora do cardápio).")
        return
    for i in achados[:5]:
        await cartao_cardapio(context.bot, update.effective_chat.id, i, "item")
    if len(achados) > 5:
        await enviar(context.bot, update.effective_chat.id, f"… e mais {len(achados) - 5}. Refine a busca.")


async def cmd_tagme_campos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tagme_campos – mostra, no privado do gestor, COMO a Tagme manda uma reserva (nomes dos campos,
    com os textos mascarados), para achar em qual campo vem o nome do cliente."""
    if not _eh_gestor(update):
        await update.effective_message.reply_text("Só o gestor pode usar /tagme_campos.")
        return
    if not TG:
        await update.effective_message.reply_text("A Tagme não está configurada.")
        return
    h = hoje()
    res = []
    for dia in (h + timedelta(days=1), h, h + timedelta(days=2)):
        try:
            res = await asyncio.wait_for(asyncio.to_thread(TG.reservas, dia), 120)
        except Exception as e:
            await enviar(context.bot, update.effective_chat.id, f"Erro ao consultar a Tagme: {esc(e)}")
            return
        if res:
            break
    if not res:
        await enviar(context.bot, update.effective_chat.id, "Não achei reservas para hoje, amanhã ou depois.")
        return

    def mascara(v: str) -> str:  # "Maria Souza" → "M**** S****" (dá para ver o formato sem expor o dado)
        return " ".join(w[:1] + "*" * min(len(w) - 1, 4) for w in str(v).split()[:4])[:40]

    def campos(o, pre="", prof=0, L=None):
        L = [] if L is None else L
        if prof > 4:
            return L
        if isinstance(o, dict):
            for k, v in o.items():
                cam = f"{pre}.{k}" if pre else k
                if isinstance(v, (dict, list)):
                    campos(v, cam, prof + 1, L)
                elif isinstance(v, str):
                    kl = k.lower()
                    livre = (re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+(Z|[+\-]\d{2}:?\d{2})?", v) or len(v) <= 3
                             or any(t in kl for t in ("status", "type", "tipo", "origin", "source", "channel", "lang")))
                    vis = v if livre else mascara(v)
                    L.append(f"{cam} = \"{vis}\"" if v.strip() else f"{cam} = (vazio)")
                else:
                    L.append(f"{cam} = {v!r}"[:80])
        elif isinstance(o, list):
            if o:
                campos(o[0], pre + "[0]", prof + 1, L)
            else:
                L.append(f"{pre} = [] (lista vazia)")
        return L

    o = res[0]
    L = [f"🔎 <b>Tagme – campos de uma reserva</b> ({len(res)} no dia; mostrando a 1ª)",
         f"Nome que o robô achou: <b>{esc(_nome_completo(o))}</b>", ""]
    L += [esc(x) for x in campos(o)[:120]]
    await enviar(context.bot, update.effective_chat.id, "\n".join(L))


async def cmd_testar_leankeep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/testar_leankeep – TESTE só de leitura da parte antiga do Leankeep: abre o formulário de
    ocorrência nova, lista grupos e áreas da unidade. Não salva nada no Leankeep."""
    if not _eh_gestor(update):
        await update.effective_message.reply_text("Só o gestor pode usar /testar_leankeep.")
        return
    if MODO != "leankeep" or not LK:
        await update.effective_message.reply_text("O robô está sem acesso ao Leankeep.")
        return
    await update.effective_message.reply_text("🔎 Abrindo o formulário de ocorrência no Leankeep (só leitura, não salva nada)…")
    import legado

    def rodar():
        if not LK.token:
            LK.login()
        try:
            return legado.testar(LK.token, LK.refresh, empresa_id=LK.empresa_id)
        except RuntimeError as e:
            if "não aceitou" not in str(e) and "não achei o formulário" not in str(e):
                raise
            log.warning("Leankeep antigo, 1ª tentativa: %s", e)
            LK.renovar()  # token velho: renova e tenta de novo
            return legado.testar(LK.token, LK.refresh, empresa_id=LK.empresa_id)

    try:
        res = await asyncio.wait_for(asyncio.to_thread(rodar), 240)
    except Exception as e:
        log.exception("Teste do Leankeep antigo falhou")
        await enviar(context.bot, update.effective_chat.id, f"⚠️ Teste do Leankeep antigo falhou: {esc(str(e)[:400])}")
        return
    gravar(ARQ_LK_AREAS, {"em": datetime.now(FUSO).isoformat(timespec="minutes"), **res})
    un = res.get("unidade") or ("?", "?")
    L = ["✅ <b>Consegui abrir o formulário de ocorrência do Leankeep</b> (nada foi salvo)", "",
         f"Unidade: <b>{esc(un[1])}</b> (código {esc(un[0])})",
         f"Solicitante que viria preenchido: <b>{esc(res.get('emitente') or '?')}</b>",
         f"Prioridades: {', '.join(res['prioridades'])}", "", "<b>Grupos de áreas e áreas</b>"]
    for chave, areas in res["grupos"].items():
        nome = chave.split("|", 1)[1]
        if isinstance(areas, str):
            L.append(f"• {esc(nome)}: {esc(areas[:400])}")
        else:
            nomes = ", ".join(a[1].split("/")[0].title() for a in areas[:12])
            L.append(f"• <b>{esc(nome)}</b> ({len(areas)}): {esc(nomes)}" + (" …" if len(areas) > 12 else ""))
    await enviar(context.bot, update.effective_chat.id, "\n".join(L))


async def cmd_apagar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responda (marque) uma mensagem com /apagar.
    • Mensagem do robô: é apagada do grupo.
    • Agradecimento do bar ou a própria postagem: a entrega registrada é desfeita (o robô volta a cobrar)."""
    msg = update.effective_message
    if not _eh_gestor(update):
        await msg.reply_text("Só o gestor pode usar /apagar.")
        return
    alvo = msg.reply_to_message
    if not alvo or (msg.is_topic_message and alvo.message_id == msg.message_thread_id):
        await msg.reply_text("Responda (marque) a mensagem que quer apagar e escreva /apagar.")
        return
    chat = msg.chat.id
    desfeitas = BAR.desfazer(chat, alvo.message_id)
    apagou, erro = False, ""
    do_robo = bool(alvo.from_user and alvo.from_user.id == context.bot.id)
    if do_robo:
        try:
            await context.bot.delete_message(chat, alvo.message_id)
            apagou = True
        except Exception as e:
            erro = str(e)
    if desfeitas:  # não apaga a foto de quem postou: só tira a reação do robô e a entrega
        post = BAR.msg_da_postagem(BAR.ULTIMO_LINK_DESFEITO[0]) if BAR.ULTIMO_LINK_DESFEITO else None
        try:
            await context.bot.set_message_reaction(chat, post or alvo.message_id, None)
        except Exception:
            pass
    try:
        await msg.delete()  # tira o próprio /apagar do grupo
    except Exception:
        pass
    L = []
    if do_robo:
        L.append("🗑️ Mensagem do robô apagada." if apagou else
                 f"⚠️ Não consegui apagar a mensagem ({esc(erro[:120])}). O robô precisa ser administrador "
                 f"com permissão de apagar mensagens.")
    if desfeitas:
        L.append("↩️ Entrega desfeita – o robô volta a cobrar se ainda estiver pendente:")
        L += [f"• {esc(x)}" for x in desfeitas]
    if not L:
        L.append("Essa mensagem não é do robô nem uma entrega registrada; não apaguei nada.")
    destino = CFG.get("gestor_chat_id") or chat
    await enviar(context.bot, destino, "\n".join(L))


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
    "• /conferir – o que o robô está vendo no Leankeep, cobrança por cobrança\n"
    "• /pendencias – lista de ocorrências em aberto\n"
    "• /resolver N – conclui a ocorrência N\n"
    "• /reativar N – reabre uma ocorrência baixada por engano\n"
    "• /excluir N – apaga a ocorrência N\n"
    "• /semana – resumo da semana passada (/semana atual = semana corrente)\n"
    "• /fila – fila de espera da Tagme agora\n"
    "• /reservas – reservas de amanhã (/reservas hoje, /reservas 25/12)\n"
    "• /alerta – manda um alerta 🚨 para o tópico que você escolher\n"
    "• /reenviar – manda de novo algo que não chegou\n"
    "• /bar – situação das rotinas do bar + farol (no privado)\n"
    "• /farol – publica o farol do bar no tópico General (/farol aqui = só para você)\n"
    "• /topico_bar CHAVE – dentro de cada tópico do grupo do bar, para configurar\n"
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
        "conf_falta": "⚠️ Resposta ao \"baixei\" (faltando)",
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
        await enviar(bot, CFG.get("grupo_chat_id"), texto_faltam_baixas("Fulano", [(cob, exemplo_ativs(cob))]))
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
        sc, st = oc_saida()
        await oc_enviar_lista(bot, sc, st or None)
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
    if await sg_resposta_privada(update, context):
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
    if not exemplo and declarada(cob):
        log.info("Lembrete '%s' não enviado: baixa declarada", lem.get("texto"))
        return None, cob
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
    import sys as _sys
    BAR.setup(_sys.modules[__name__])
    BAR.registrar_handlers(app)
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
    app.add_handler(CallbackQueryHandler(oc_botao_pergunta, pattern=r"^ocq\|"))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, mensagem_grupo))
    app.add_handler(CommandHandler("alerta", cmd_alerta))
    app.add_handler(CommandHandler("conferir", cmd_conferir))
    app.add_handler(CallbackQueryHandler(botao_baixa, pattern=r"^bx\|"))
    app.add_handler(CommandHandler("reativar", cmd_reativar))
    app.add_handler(CommandHandler("apagar", cmd_apagar))
    app.add_handler(CommandHandler("testar_leankeep", cmd_testar_leankeep))
    app.add_handler(CommandHandler("tagme_campos", cmd_tagme_campos))
    app.add_handler(CommandHandler("cardapio", cmd_cardapio))
    app.add_handler(CommandHandler("configurar_cardapio", cmd_configurar_cardapio))
    app.add_handler(CommandHandler("registrar_leankeep", cmd_registrar_leankeep))
    app.add_handler(CallbackQueryHandler(lk_botao, pattern=r"^lk\|"))
    app.add_handler(CommandHandler("auditoria", cmd_auditoria))
    app.add_handler(CommandHandler("seguimento", cmd_seguimento))
    app.add_handler(CallbackQueryHandler(sg_botao, pattern=r"^sg\|"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.PHOTO & filters.REPLY, sg_foto_privada))
    app.add_handler(CallbackQueryHandler(aud_botao, pattern=r"^au\|"))
    app.add_handler(CommandHandler("cardapio_item", cmd_cardapio_item))
    app.add_handler(CallbackQueryHandler(botao_cardapio, pattern=r"^cd\|"))
    app.add_handler(CallbackQueryHandler(botao_reativar, pattern=r"^ocr\|"))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CallbackQueryHandler(botao_alerta, pattern=r"^al\|"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, msg_privada))
    return app


if __name__ == "__main__":
    main()
