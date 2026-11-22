"""Cobranças do BAR (grupo "CB Jundiaí - Bar"), conforme o Guide de Procedimentos Bar.

Tudo vem do config.toml (bloco [bar]):
  - cada rotina tem um tópico, um tipo (diaria, semanal, mensal, quinzenal, diretor) e um prazo;
  - a evidência é a POSTAGEM no tópico (foto, arquivo ou texto longo). Em tópico com mais de uma
    rotina, o robô responde com botões para a pessoa dizer o que postou;
  - quem entrega encerra a cobrança; enquanto não entrega, sobe o tom: lembrete → cobrança → alerta;
  - horário de silêncio (padrão 01:30–09:00): o que cair nele sai às 09:00, se ainda estiver pendente;
  - farol 🟢🟡🔴 das rotinas quinzenais e mensais no tópico General.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode

B = None  # módulo do robô (bot.py), ligado por setup()

DIAS = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "dom": 6}
NOMES_DIA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
NIVEL_ORDEM = {"lembrete": 1, "cobranca": 2, "alerta": 3}


def setup(modulo_bot):
    global B
    B = modulo_bot


# ---------------- configuração ----------------
def cfg() -> dict:
    return B.CFG.get("bar", {}) or {}


def rotinas() -> list[dict]:
    return [r for r in cfg().get("rotinas", []) if r.get("ativo", True)]


def rot(rid: str) -> dict | None:
    return next((r for r in rotinas() if r["id"] == rid), None)


def arq():
    return B.DADOS / "bar.json"


def dados() -> dict:
    d = B.ler(arq(), {})
    d.setdefault("evid", [])
    d.setdefault("enviados", {})
    d.setdefault("classificar", {})
    return d


def gravar(d: dict):
    # guarda só o necessário: evidências de ~200 dias e marcas de envio de ~70 dias
    lim_e = (B.hoje() - timedelta(days=200)).isoformat()
    lim_m = (B.hoje() - timedelta(days=70)).isoformat()
    d["evid"] = [e for e in d["evid"] if e.get("em", "") >= lim_e]
    d["enviados"] = {k: v for k, v in d["enviados"].items() if v >= lim_m}
    B.gravar(arq(), d)


def topicos() -> dict:
    """chave do tópico -> [chat_id, thread_id] (gravado pelo /topico_bar)."""
    return B.CFG.get("bar_topicos", {}) or {}


def destino(chave: str):
    t = topicos().get(chave) or topicos().get("general")
    if t:
        return int(t[0]), (int(t[1]) or None)
    return B.CFG.get("gestor_chat_id"), None


def chave_do_topico(chat_id, thread) -> str | None:
    for k, (c, t) in topicos().items():
        if int(c) == int(chat_id) and int(t or 0) == int(thread or 0):
            return k
    return None


def no_grupo_bar(chat_id) -> bool:
    return any(int(c) == int(chat_id) for c, _ in topicos().values())


def hm(txt: str) -> time:
    h, m = map(int, str(txt).split(":"))
    return time(h, m)


def em(d: date, t: time | str) -> datetime:
    if isinstance(t, str):
        t = hm(t)
    return datetime.combine(d, t, tzinfo=B.FUSO)


def ultimo_dia(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


# ---------------- períodos ----------------
def periodo(r: dict, quando: datetime):
    """(chave, prazo_final, rótulo) do período a que 'quando' pertence."""
    tipo = r.get("tipo", "diaria")
    freq = r.get("frequencia", tipo)  # rotinas do diretor usam a frequência
    if tipo == "diaria" or freq == "diaria":
        t = hm(r.get("prazo", "23:59"))
        if t < time(6, 0):  # prazo de madrugada pertence ao dia anterior
            base = quando.date() - timedelta(days=1) if quando.time() < time(6, 0) else quando.date()
            fim = em(base + timedelta(days=1), t)
        else:
            base = quando.date()
            fim = em(base, t)
        return base.isoformat(), fim, base.strftime("%d/%m")
    if freq == "semanal":
        seg = quando.date() - timedelta(days=quando.weekday())
        fim = em(seg + timedelta(days=DIAS[r.get("dia", "seg")]), r.get("prazo", "23:59"))
        ano, sem, _ = seg.isocalendar()
        return f"{ano}-S{sem:02d}", fim, f"semana de {seg:%d/%m}"
    if freq == "quinzenal":
        d = quando.date()
        if d.day <= 15:
            return f"{d:%Y-%m}-Q1", em(d.replace(day=15), r.get("prazo", "23:59")), f"1ª quinzena de {d:%m/%Y}"
        return f"{d:%Y-%m}-Q2", em(ultimo_dia(d), r.get("prazo", "23:59")), f"2ª quinzena de {d:%m/%Y}"
    if freq == "semestral":
        d = quando.date()
        if d.month <= 6:
            return f"{d.year}-S1", em(date(d.year, 6, 30), "23:59"), f"1º semestre de {d.year}"
        return f"{d.year}-S2", em(date(d.year, 12, 31), "23:59"), f"2º semestre de {d.year}"
    d = quando.date()  # mensal
    return f"{d:%Y-%m}", em(ultimo_dia(d), r.get("prazo", "23:59")), f"{d:%m/%Y}"


def periodo_anterior(r: dict, quando: datetime):
    tipo = r.get("frequencia", r.get("tipo", "diaria"))
    chave, fim, _ = periodo(r, quando)
    if tipo == "diaria":
        return periodo(r, fim - timedelta(days=1, hours=1))
    if tipo == "semanal":
        return periodo(r, quando - timedelta(days=7))
    ini = {"quinzenal": 16, "mensal": 32, "semestral": 190}.get(tipo, 32)
    return periodo(r, fim - timedelta(days=ini))


def entregas(d: dict, rid: str, pchave: str) -> list[dict]:
    return [e for e in d["evid"] if e["rotina"] == rid and e["periodo"] == pchave]


def meta(r: dict) -> int:
    return int(r.get("meta", 1))


# ---------------- quando cobrar ----------------
def silencio():
    s = cfg().get("silencio", ["01:30", "09:00"])
    return hm(s[0]), hm(s[1])


def ajustar_silencio(dt: datetime) -> datetime:
    ini, fim = silencio()
    t = dt.timetz().replace(tzinfo=None)
    if ini <= t < fim:
        return em(dt.date(), fim)
    return dt


def marcos(r: dict, pchave: str, fim: datetime, feitos: int) -> list[tuple[datetime, str]]:
    """Momentos de lembrete/cobrança/alerta do período, já com o horário de silêncio aplicado.
    As condições de meta parcial (dia 10 / dia 20) são aplicadas aqui."""
    tipo = r.get("tipo", "diaria")
    freq = r.get("frequencia", tipo)
    m = meta(r)
    L = []
    if tipo == "diretor":
        if freq == "quinzenal":
            L = [(fim - timedelta(days=5), "lembrete"), (fim, "cobranca")]
        elif freq == "semestral":
            L = [(em(fim.date().replace(day=1), "10:00"), "lembrete"),
                 (em(fim.date().replace(day=15), "10:00"), "cobranca")]
        else:
            L = [(em(fim.date().replace(day=20), "10:00"), "lembrete"),
                 (em(fim.date().replace(day=25), "10:00"), "cobranca")]
        # depois: uma vez por semana até marcar Feito (até 5 semanas)
        L += [(fim + timedelta(days=7 * k), "alerta") for k in range(0, 5)]
    elif freq == "diaria":
        L = [(fim - timedelta(minutes=30), "lembrete"), (fim, "cobranca"),
             (fim + timedelta(minutes=30), "alerta"), (fim + timedelta(minutes=60), "alerta")]
    elif freq == "semanal":
        dia = fim.date()
        L = [(em(dia - timedelta(days=1), "10:00"), "lembrete")]
        if em(dia, "10:00") < fim:
            L.append((em(dia, "10:00"), "lembrete"))
        L.append((fim, "cobranca"))
        domingo = dia + timedelta(days=6 - dia.weekday())
        k = dia + timedelta(days=1)
        while k <= domingo:
            L.append((em(k, "10:00"), "alerta"))
            k += timedelta(days=1)
    elif freq == "quinzenal":
        dia = fim.date()
        L = [(em(dia - timedelta(days=5), "10:00"), "lembrete"), (fim if fim.time() != time(23, 59) else em(dia, "10:00"), "cobranca")]
        for k in range(1, 6):
            L.append((em(dia + timedelta(days=k), "10:00"), "alerta"))
    else:  # mensal
        ult = fim.date()
        ini = ult.replace(day=1)
        if feitos == 0:
            L.append((em(ini.replace(day=10), "10:00"), "lembrete"))
        if feitos * 2 < m or (m == 1 and feitos == 0):
            L.append((em(ini.replace(day=20), "10:00"), "cobranca"))
        d = ini.replace(day=25)
        while d <= ult:
            if (ult - d).days <= 1:  # 2 últimos dias: alerta de manhã e à tarde
                L += [(em(d, "10:00"), "alerta"), (em(d, "16:00"), "alerta")]
            else:
                L.append((em(d, "10:00"), "cobranca"))
            d += timedelta(days=1)
    vistos, saida = set(), []
    for dt, nv in sorted(L):
        dt = ajustar_silencio(dt)
        if (dt, nv) not in vistos:
            vistos.add((dt, nv))
            saida.append((dt, nv))
    return saida


# ---------------- textos ----------------
def nome_topico(chave: str) -> str:
    return (cfg().get("nomes_topicos", {}) or {}).get(chave, chave)


def situacao(r: dict, feitos: int) -> str:
    m = meta(r)
    if m <= 1:
        return "pendente"
    return f"{feitos} de {m} (falta {m - feitos})"


def texto_cobranca(r: dict, nivel: str, rotulo: str, fim: datetime, feitos: int) -> str:
    prazo = (f"hoje às {fim:%H:%M}" if fim.date() == B.hoje() else
             f"{NOMES_DIA[fim.weekday()]} {fim:%d/%m} às {fim:%H:%M}")
    if fim < datetime.now(B.FUSO):
        prazo = f"venceu {fim:%d/%m} às {fim:%H:%M}"
    if r.get("tipo") in ("mensal", "quinzenal") and fim.time() == time(23, 59):
        prazo = f"até {fim:%d/%m}"
    onde = (f"Poste a evidência no tópico <b>{B.esc(nome_topico(r['topico']))}</b>"
            + (f" e toque em <b>{B.esc(r['botao'])}</b>" if r.get("botao") else "")
            + ", ou toque em Feito abaixo.") if r.get("tipo") != "diretor" else "Quando concluir, toque em Feito."
    if nivel == "lembrete":
        L = [f"⏰ <b>{B.esc(r['nome'])}</b>", "",
             f"Período: {B.esc(rotulo)} · prazo {prazo}"]
        if meta(r) > 1:
            L.append(f"Situação: {situacao(r, feitos)}")
        L += ["", onde]
        return "\n".join(L)
    L = [f"🚨🚨 <b>{B.esc(r['nome'].upper())}</b> 🚨🚨", "",
         f"Período: {B.esc(rotulo)} · prazo {prazo}",
         f"Situação: {situacao(r, feitos)}", "", onde]
    marcar = r.get("marcar", cfg().get("marcar", []))
    if marcar and r.get("tipo") != "diretor":
        L += ["", B.marcacoes(marcar)]
    return "\n".join(L)


def texto_agrupado(topico: str, itens) -> tuple[str, InlineKeyboardMarkup | None]:
    nivel = max((it[1] for it in itens), key=lambda n: NIVEL_ORDEM[n])
    titulo = f"PENDÊNCIAS – {nome_topico(topico).upper()}"
    L = [f"⏰ <b>{B.esc(titulo)}</b>" if nivel == "lembrete" else f"🚨🚨 <b>{B.esc(titulo)}</b> 🚨🚨", ""]
    for r, nv, rotulo, fim, feitos, pchave in itens:
        prazo = f"até {fim:%d/%m}" if fim.time() == time(23, 59) else f"prazo {fim:%d/%m %H:%M}"
        if fim < datetime.now(B.FUSO):
            prazo = f"venceu {fim:%d/%m %H:%M}" if fim.time() != time(23, 59) else f"venceu {fim:%d/%m}"
        L.append(f"• <b>{B.esc(r.get('nome_curto', r['nome']))}</b>: {situacao(r, feitos)} · {prazo}")
    L += ["", "Poste a evidência aqui e toque no botão do que foi postado, ou toque em Feito abaixo."]
    if nivel != "lembrete":
        marcar = itens[0][0].get("marcar", cfg().get("marcar", []))
        if marcar:
            L += ["", B.marcacoes(marcar)]
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Feito: {r.get('nome_curto', r['nome'])[:28]}",
                                                         callback_data=f"bf|{r['id']}|{pchave}")]
                                   for r, _, _, _, _, pchave in itens])
        return "\n".join(L), kb
    return "\n".join(L), None


def botao_feito(r: dict, pchave: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Feito", callback_data=f"bf|{r['id']}|{pchave}")]])


# ---------------- ciclo (roda a cada rodada / 5 min) ----------------
async def tick(context):
    if not cfg().get("ativo", True) or not rotinas() or not topicos():
        return  # só começa depois que algum tópico do bar for configurado com /topico_bar
    bot = context.bot
    agora = datetime.now(B.FUSO)
    d = dados()
    ultima = d.get("ultimo_tick")
    desde = datetime.fromisoformat(ultima) if ultima else agora - timedelta(minutes=10)
    desde = max(desde, agora - timedelta(hours=6))  # rodada muito atrasada não despeja tudo
    g = B.CFG.get("gestor_chat_id")
    fila = {}
    inicio = datetime.fromisoformat(d.setdefault("inicio", agora.isoformat(timespec="seconds")))

    for r in rotinas():
        for pchave, fim, rotulo in {periodo(r, agora), periodo_anterior(r, agora)}:
            if fim < inicio:  # períodos que venceram antes do robô do bar começar não são cobrados
                continue
            feitos = len(entregas(d, r["id"], pchave))
            if feitos >= meta(r):
                await checar_palavra(bot, d, r, pchave, fim, agora, desde)
                continue
            devidos = [(dt, nv) for dt, nv in marcos(r, pchave, fim, feitos) if desde < dt <= agora]
            if not devidos:
                continue
            dt, nivel = max(devidos, key=lambda x: (x[0], NIVEL_ORDEM[x[1]]))
            marca = f"{r['id']}|{pchave}|{dt:%Y-%m-%dT%H:%M}|{nivel}"
            if marca in d["enviados"]:
                continue
            d["enviados"][marca] = agora.date().isoformat()
            if r.get("tipo") == "diretor":
                await B.enviar(bot, g, texto_cobranca(r, nivel, rotulo, fim, feitos),
                               reply_markup=botao_feito(r, pchave))
                continue
            if r["topico"] not in topicos() and "general" not in topicos():
                continue  # tópico ainda não configurado
            fila.setdefault(r["topico"], []).append((r, nivel, rotulo, fim, feitos, pchave))

    # uma mensagem por tópico (várias pendências do mesmo tópico saem juntas)
    alertas_privado = []
    for topico, itens in fila.items():
        chat, top = destino(topico)
        if len(itens) == 1:
            r, nivel, rotulo, fim, feitos, pchave = itens[0]
            kb = botao_feito(r, pchave) if nivel != "lembrete" else None
            await B.enviar(bot, chat, texto_cobranca(r, nivel, rotulo, fim, feitos), message_thread_id=top,
                           **({"reply_markup": kb} if kb else {}))
        else:
            texto, kb = texto_agrupado(topico, itens)
            await B.enviar(bot, chat, texto, message_thread_id=top, **({"reply_markup": kb} if kb else {}))
        alertas_privado += [(r, rotulo, feitos) for r, nivel, rotulo, fim, feitos, _ in itens if nivel == "alerta"]
    if alertas_privado and g:
        L = ["🚨 <b>Bar – continua pendente</b>", ""]
        L += [f"• {B.esc(r.get('nome_curto', r['nome']))} ({B.esc(rotulo)}): {situacao(r, f)}"
              for r, rotulo, f in alertas_privado]
        await B.enviar(bot, g, "\n".join(L))

    await lembrar_classificacao(bot, d, agora)
    await farol_programado(bot, d, agora, desde)
    await resumo_noite(bot, d, agora, desde)
    d["ultimo_tick"] = agora.isoformat(timespec="seconds")
    gravar(d)


async def checar_palavra(bot, d, r, pchave, fim, agora, desde):
    """Ex.: desperdício entregue sem 'chopp' → avisa uma vez, no prazo."""
    palavra = r.get("palavra_obrigatoria")
    if not palavra or not (desde < fim <= agora):
        return
    marca = f"{r['id']}|{pchave}|palavra"
    if marca in d["enviados"]:
        return
    alvo = B.normalizar(palavra)
    if any(alvo in B.normalizar(e.get("texto", "")) for e in entregas(d, r["id"], pchave)):
        return
    d["enviados"][marca] = agora.date().isoformat()
    chat, top = destino(r["topico"])
    await B.enviar(bot, chat, f"⚠️ <b>{B.esc(r['nome'])}</b>\n\nO registro de hoje não menciona "
                              f"<b>{B.esc(palavra)}</b>, que é obrigatório. Complete o registro, por favor.\n\n"
                              + B.marcacoes(r.get("marcar", cfg().get("marcar", []))),
                   message_thread_id=top)


# ---------------- registro das evidências ----------------
def link_msg(msg) -> str:
    cid = str(msg.chat.id)
    base = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    link = f"https://t.me/c/{base}/{msg.message_id}"
    if msg.is_topic_message and msg.message_thread_id:
        link += f"?thread={msg.message_thread_id}"
    return link


def registrar(d: dict, r: dict, quando: datetime, quem: str, via: str, link: str = "", texto: str = ""):
    pchave, _, rotulo = periodo(r, quando)
    d["evid"].append({"rotina": r["id"], "periodo": pchave, "em": quando.isoformat(timespec="minutes"),
                      "quem": quem, "via": via, "link": link, "texto": texto[:300]})
    return pchave, rotulo


def vale_como_evidencia(msg) -> bool:
    if msg.photo or msg.video or msg.document:
        return True
    texto = (msg.text or "").strip()
    return len(texto) >= int(cfg().get("min_caracteres_texto", 40))


async def on_mensagem(update: Update, context) -> bool:
    """Mensagem em grupo: se for num tópico do bar, trata. Devolve True se era do bar."""
    msg = update.effective_message
    if not msg or not topicos() or not no_grupo_bar(msg.chat.id):
        return False
    chave = chave_do_topico(msg.chat.id, msg.message_thread_id if msg.is_topic_message else 0)
    if not chave:
        return True
    cands = [r for r in rotinas() if r.get("topico") == chave and r.get("tipo") != "diretor"]
    if not cands or not vale_como_evidencia(msg):
        return True
    if msg.media_group_id:  # álbum: só a 1ª foto conta
        d = dados()
        if msg.media_group_id in d.get("albuns", {}):
            return True
        d.setdefault("albuns", {})[msg.media_group_id] = B.hoje().isoformat()
        d["albuns"] = dict(list(d["albuns"].items())[-50:])
        gravar(d)
    quando = msg.date.astimezone(B.FUSO) if msg.date else datetime.now(B.FUSO)
    quem = msg.from_user.full_name if msg.from_user else "?"
    texto = (msg.text or msg.caption or "").strip()
    d = dados()
    if len(cands) == 1:
        r = cands[0]
        registrar(d, r, quando, quem, "postagem", link_msg(msg), texto)
        gravar(d)
        await B.reagir(context.bot, msg)
        return True
    d["classificar"][str(msg.message_id)] = {
        "chat": msg.chat.id, "thread": msg.message_thread_id if msg.is_topic_message else 0,
        "uid": msg.from_user.id if msg.from_user else 0, "quem": quem,
        "em": quando.isoformat(timespec="minutes"), "texto": texto[:300], "link": link_msg(msg), "lembrado": False}
    gravar(d)
    botoes = [[InlineKeyboardButton(r.get("botao") or r["nome"][:30], callback_data=f"bc|{msg.message_id}|{r['id']}")]
              for r in cands]
    await B.enviar(context.bot, msg.chat.id, "📌 O que foi postado? Toque na opção certa:",
                   message_thread_id=msg.message_thread_id if msg.is_topic_message else None,
                   reply_to_message_id=msg.message_id, reply_markup=InlineKeyboardMarkup(botoes))
    return True


async def lembrar_classificacao(bot, d, agora):
    for mid, p in list(d["classificar"].items()):
        idade = agora - datetime.fromisoformat(p["em"])
        if idade > timedelta(days=2):
            del d["classificar"][mid]
        elif idade > timedelta(minutes=30) and not p.get("lembrado"):
            p["lembrado"] = True
            quem = f'<a href="tg://user?id={p["uid"]}">{B.esc(p["quem"].split(" ")[0])}</a>' if p.get("uid") else ""
            await B.enviar(bot, p["chat"], f"📌 {quem}, falta tocar no botão para dizer o que você postou. "
                                           f"Sem isso, a entrega não conta.",
                           message_thread_id=p["thread"] or None, reply_to_message_id=int(mid))


async def botao_classificar(update: Update, context):
    q = update.callback_query
    _, mid, rid = q.data.split("|", 2)
    d = dados()
    p = d["classificar"].get(mid)
    r = rot(rid)
    if not p or not r:
        await q.answer("Essa postagem já foi registrada.")
        return
    del d["classificar"][mid]
    quando = datetime.fromisoformat(p["em"])
    pchave, rotulo = registrar(d, r, quando, p["quem"], f"postagem (botão de {q.from_user.full_name})",
                               p["link"], p["texto"])
    gravar(d)
    feitos = len(entregas(d, rid, pchave))
    await q.answer("Registrado!")
    extra = f" · {feitos} de {meta(r)} no período" if meta(r) > 1 else ""
    try:
        await q.edit_message_text(B.limpar_texto(f"✅ Registrado como <b>{B.esc(r['nome'])}</b> "
                                                 f"({B.esc(rotulo)}){extra}."), parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def botao_feito_bar(update: Update, context):
    q = update.callback_query
    _, rid, pchave = q.data.split("|", 2)
    r = rot(rid)
    if not r:
        await q.answer("Essa rotina não existe mais.")
        return
    if r.get("tipo") == "diretor" and not B._eh_gestor(update):
        await q.answer("Só o diretor pode marcar.", show_alert=True)
        return
    d = dados()
    quando = datetime.now(B.FUSO)
    # registra no período da cobrança (mesmo que o toque seja depois)
    d["evid"].append({"rotina": rid, "periodo": pchave, "em": quando.isoformat(timespec="minutes"),
                      "quem": q.from_user.full_name, "via": "botão Feito", "link": "", "texto": ""})
    gravar(d)
    feitos = len(entregas(d, rid, pchave))
    await q.answer("Anotado!")
    resto = "" if feitos >= meta(r) else f" Ainda falta {meta(r) - feitos}."
    try:
        await q.edit_message_text(B.limpar_texto(
            f"✅ <b>{B.esc(r['nome'])}</b>: marcado como feito por {B.esc(q.from_user.full_name)} "
            f"às {quando:%H:%M}.{resto}"), parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ---------------- farol ----------------
def cor(feitos: int, m: int, ultimos_dias: bool) -> str:
    if feitos >= m:
        return "🟢"
    if feitos > 0 and not ultimos_dias:
        return "🟡"
    return "🔴"


def texto_farol(d: dict, agora: datetime) -> tuple[str, bool]:
    hoje = agora.date()
    ult = ultimo_dia(hoje)
    ultimos = (ult - hoje).days <= 1
    meses = ["", "JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO", "JULHO", "AGOSTO",
             "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO"]
    L = [f"🚦 <b>FAROL DO BAR – {meses[hoje.month]}</b> (atualizado {hoje:%d/%m})", ""]
    vermelho = False
    for r in rotinas():
        tipo = r.get("tipo")
        if tipo == "mensal":
            pch, _, _ = periodo(r, agora)
            f = len(entregas(d, r["id"], pch))
            c = cor(f, meta(r), ultimos)
            vermelho |= c == "🔴"
            L.append(f"{B.esc(r.get('nome_curto', r['nome']))}: {f} de {meta(r)} {c}")
        elif tipo == "quinzenal":
            for q, dia_fim in (("Q1", 15), ("Q2", ult.day)):
                if q == "Q2" and hoje.day <= 15:
                    continue
                pch = f"{hoje:%Y-%m}-{q}"
                f = len(entregas(d, r["id"], pch))
                fim_q = hoje.replace(day=dia_fim)
                ultimos_q = (fim_q - hoje).days <= 1
                c = cor(f, meta(r), ultimos_q)
                vermelho |= c == "🔴"
                status = "entregue" if f >= meta(r) else f"até {fim_q:%d/%m}"
                L.append(f"{B.esc(r.get('nome_curto', r['nome']))} {1 if q == 'Q1' else 2}ª quinzena: {status} {c}")
    faltam = (ult - hoje).days
    L += ["", "Último dia do mês." if faltam == 0 else f"Faltam {faltam} dia(s) para o fim do mês."]
    if vermelho:
        L.append(B.marcacoes(cfg().get("marcar", [])))
    return "\n".join(L), vermelho


async def enviar_farol(bot, d, agora, chat=None, thread=None):
    texto, _ = texto_farol(d, agora)
    if chat is None:
        chat, thread = destino("general")
    await B.enviar(bot, chat, texto, message_thread_id=thread)


async def farol_programado(bot, d, agora, desde):
    f = cfg().get("farol", {}) or {}
    if not f.get("ativo", True):
        return
    hoje = agora.date()
    horarios = []
    for dia_txt in f.get("dias", ["seg"]):
        if hoje.weekday() == DIAS[dia_txt]:
            horarios.append(f.get("horario", "10:00"))
    if hoje.day >= int(f.get("reforco_a_partir_do_dia", 25)):
        for dia_txt in f.get("dias_reforco", ["qui"]):
            if hoje.weekday() == DIAS[dia_txt]:
                horarios.append(f.get("horario", "10:00"))
    if hoje == ultimo_dia(hoje):
        horarios.append(f.get("horario_fechamento", "20:00"))
    for h in set(horarios):
        dt = em(hoje, h)
        marca = f"farol|{hoje}|{h}"
        if desde < dt <= agora and marca not in d["enviados"]:
            d["enviados"][marca] = hoje.isoformat()
            await enviar_farol(bot, d, agora)


# ---------------- resumo da noite (privado do diretor) ----------------
def texto_resumo(d: dict, base: date) -> str:
    L = [f"🌙 <b>Bar – resumo de {base:%d/%m}</b>", ""]
    for r in rotinas():
        if r.get("tipo") != "diaria":
            continue
        es = entregas(d, r["id"], base.isoformat())
        if es:
            e = es[0]
            L.append(f"{B.esc(r.get('nome_curto', r['nome']))}: {B.esc(e['quem'].split(' ')[0])} "
                     f"às {e['em'][11:16]} ✅")
        else:
            L.append(f"{B.esc(r.get('nome_curto', r['nome']))}: não entregue ❌")
    return "\n".join(L)


async def resumo_noite(bot, d, agora, desde):
    h = cfg().get("resumo_noite", "01:20")
    if not h:
        return
    dt = em(agora.date(), h)
    base = (dt - timedelta(hours=6)).date()
    marca = f"resumo|{base}"
    if desde < dt <= agora and marca not in d["enviados"] and B.CFG.get("gestor_chat_id"):
        d["enviados"][marca] = agora.date().isoformat()
        await B.enviar(bot, B.CFG["gestor_chat_id"], texto_resumo(d, base))


# ---------------- comandos ----------------
async def cmd_topico_bar(update: Update, context):
    msg = update.message
    nomes = cfg().get("nomes_topicos", {}) or {}
    if not B._eh_gestor(update):
        await msg.reply_text("Só o gestor pode configurar os tópicos do bar.")
        return
    if update.effective_chat.type == "private" or not context.args:
        await msg.reply_text("Envie DENTRO do tópico, com a chave dele. Exemplo: /topico_bar relatorios\n\n"
                             "Chaves: " + ", ".join(f"{k} ({v})" for k, v in nomes.items()))
        return
    chave = context.args[0].strip().lower()
    if chave not in nomes:
        await msg.reply_text("Chave desconhecida. Use uma destas: " + ", ".join(nomes))
        return
    top = dict(topicos())
    top[chave] = [update.effective_chat.id, msg.message_thread_id if msg.is_topic_message else 0]
    B.salvar_ajuste(bar_topicos=top)
    rs = [r["nome"] for r in rotinas() if r.get("topico") == chave]
    await B.enviar(context.bot, update.effective_chat.id,
                   f"✅ Tópico <b>{B.esc(nomes[chave])}</b> configurado."
                   + ("\n\nO robô vai acompanhar aqui:\n" + "\n".join(f"• {B.esc(n)}" for n in rs) if rs else ""),
                   message_thread_id=msg.message_thread_id if msg.is_topic_message else None)


async def cmd_bar(update: Update, context):
    """/bar no privado: situação de todas as rotinas agora + farol."""
    if not B._eh_gestor(update):
        await update.message.reply_text("Só o gestor pode usar /bar.")
        return
    agora = datetime.now(B.FUSO)
    d = dados()
    nomes = cfg().get("nomes_topicos", {}) or {}
    L = [f"🍸 <b>Bar – situação agora</b> ({agora:%d/%m %H:%M})", ""]
    falta_top = [nomes.get(k, k) for k in sorted({r["topico"] for r in rotinas() if r.get("topico")})
                 if k not in topicos()]
    for tipo, titulo in (("diaria", "Diárias"), ("semanal", "Semanais"), ("quinzenal", "Quinzenais"),
                         ("mensal", "Mensais"), ("diretor", "Só do diretor")):
        rs = [r for r in rotinas() if r.get("tipo") == tipo]
        if not rs:
            continue
        L += ["", f"<b>{titulo}</b>"]
        for r in rs:
            pch, fim, rotulo = periodo(r, agora)
            f = len(entregas(d, r["id"], pch))
            ok = f >= meta(r)
            L.append(f"{B.esc(r.get('nome_curto', r['nome']))}: {f}/{meta(r)} · prazo {fim:%d/%m %H:%M} "
                     f"{'✅' if ok else '⬜'}")
    if falta_top:
        L += ["", "⚠️ Tópicos ainda não configurados (/topico_bar dentro de cada um):",
              ", ".join(B.esc(x) for x in falta_top)]
    await B.enviar(context.bot, update.effective_chat.id, "\n".join(L))
    await enviar_farol(context.bot, d, agora, chat=update.effective_chat.id)


async def cmd_farol(update: Update, context):
    """/farol – publica o farol agora no tópico General (ou /farol aqui, para ver no privado)."""
    if not B._eh_gestor(update):
        await update.message.reply_text("Só o gestor pode publicar o farol.")
        return
    d = dados()
    agora = datetime.now(B.FUSO)
    if context.args and context.args[0].lower().startswith("aqui"):
        await enviar_farol(context.bot, d, agora, chat=update.effective_chat.id)
        return
    await enviar_farol(context.bot, d, agora)
    await update.message.reply_text("✅ Farol publicado no tópico General do bar.")


def registrar_handlers(app):
    from telegram.ext import CallbackQueryHandler, CommandHandler
    app.add_handler(CommandHandler("topico_bar", cmd_topico_bar))
    app.add_handler(CommandHandler("bar", cmd_bar))
    app.add_handler(CommandHandler("farol", cmd_farol))
    app.add_handler(CallbackQueryHandler(botao_classificar, pattern=r"^bc\|"))
    app.add_handler(CallbackQueryHandler(botao_feito_bar, pattern=r"^bf\|"))
