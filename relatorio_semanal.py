"""Relatório semanal de atividades do Leankeep (o mesmo da PLANILHA ANÁLISE - DIRETORES).

1) pede ao Leankeep o "Relatório de atividades" em Excel (igual a Relatórios › Atividades › Exportar);
2) espera ficar pronto (Relatórios gerados) e baixa o arquivo;
3) classifica cada atividade como a planilha faz (OK / ATRASADO / ANTECIPADO / NÃO REALIZADO);
4) monta a planilha (RESUMO com os 4 gráficos + BASE) e a imagem-resumo.
"""
from __future__ import annotations

import io
import json
import logging
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("relatorio")
LAS = "https://las.lkp.app.br"
FUSO = ZoneInfo("America/Sao_Paulo")
STATUS_TODOS = [1, 2, 3, 4, 5]  # Pendente, Aguardando aprovação, Realizado, Não realizado, Baixado automaticamente
SITUACOES = ["OK", "ATRASADO", "ANTECIPADO", "NÃO REALIZADO"]


# =====================================================================
#  1-2) Leankeep: pedir, acompanhar e baixar
# =====================================================================
def _req(lk, metodo: str, caminho: str, **kw):
    """Chamada ao Leankeep com o token do robô; se o token venceu, renova e tenta 1x."""
    for tentativa in (1, 2):
        if not lk.token:
            lk.login()
        h = {"Authorization": f"Bearer {lk.token}", "EmpresaId": str(lk.empresa_id),
             "content-type": "application/json"}
        r = httpx.request(metodo, LAS + caminho, headers=h, timeout=120, **kw)
        if r.status_code == 401 and tentativa == 1:
            try:
                lk.renovar()
            except Exception:
                lk.login()
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Leankeep respondeu {r.status_code} em {caminho}: {r.text[:200]}")
        return r
    raise RuntimeError("Leankeep recusou o acesso")


def _meia_noite_utc(d: date) -> str:
    """Mesmo formato do site: a data escolhida (meia-noite de Brasília) em ISO/UTC."""
    return datetime.combine(d, time(0, 0), tzinfo=FUSO).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def pedir(lk, inicio: date, fim: date, login: str, unidade_id: int) -> str | None:
    corpo = {"login": login, "empresa": int(lk.empresa_id), "dataInicial": _meia_noite_utc(inicio),
             "dataFinal": _meia_noite_utc(fim), "status": STATUS_TODOS, "listaSites": [int(unidade_id)],
             "planosInativos": False}
    r = _req(lk, "POST", "/v1/Relatorios/RelatorioAtividade", content=json.dumps(corpo))
    try:
        j = r.json()
    except Exception:
        j = {}
    rid = (j.get("reportId") or j.get("id") or (j.get("data") or {}).get("reportId")) if isinstance(j, dict) else None
    log.info("Relatório pedido ao Leankeep (%s a %s): id=%s", inicio, fim, rid)
    return str(rid) if rid else None


def gerados(lk, usuario_id: int) -> list[dict]:
    r = _req(lk, "GET", f"/v1/Relatorios/ObterRelatoriosGerados?tipo=3&tipo=4&usuarioId={int(usuario_id)}")
    j = r.json()
    if isinstance(j, dict):
        j = j.get("data") or j.get("itens") or []
    return [x for x in j if isinstance(x, dict)]


def achar(lista: list[dict], report_id: str | None, pedido_em: datetime, inicio: date | None = None,
          fim: date | None = None) -> dict | None:
    """O relatório que pedimos: pelo id; se não veio id, o Excel do mesmo período emitido depois do pedido."""
    if report_id:
        for x in lista:
            if str(x.get("id")) == str(report_id):
                return x
    alvo = f"{inicio:%d/%m/%Y} - {fim:%d/%m/%Y}" if inicio and fim else None
    cands = []
    for x in lista:
        if x.get("tipo") != 3 or (alvo and str(x.get("periodo", "")).strip() != alvo):
            continue
        try:
            dt = datetime.fromisoformat(str(x.get("emissao") or "").replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=FUSO)
        except Exception:
            continue
        if dt >= pedido_em - timedelta(minutes=3):
            cands.append((dt, x))
    return max(cands, key=lambda c: c[0])[1] if cands else None


def situacao_item(x: dict) -> str:
    """'pronto', 'erro', 'sem_dados' ou 'gerando' (mesma regra da tela Relatórios gerados)."""
    st = x.get("status")
    if st == 2:
        return "pronto" if (x.get("quantidadeAtividades") or 0) > 0 or x.get("listaArquivos") else "sem_dados"
    if st == 4:
        return "erro"
    if st == 5:
        return "sem_dados"   # "NÃO GERADO (SEM DADOS)"
    return "gerando"


def baixar(lk, x: dict) -> bytes:
    nomes = json.loads(x["listaArquivos"]) if x.get("listaArquivos") else [x.get("nomeArquivo")]
    if x.get("urlRelatorio"):
        arq = httpx.get(x["urlRelatorio"], timeout=300, follow_redirects=True)
        arq.raise_for_status()
        return arq.content
    nome = [n for n in nomes if n][0]
    r = _req(lk, "POST", "/v1/Relatorios/ObterUrlRelatorio", params={"nomeArquivo": nome, "tipo": x.get("tipo", 3)})
    try:
        url = r.json()
        if isinstance(url, dict):
            url = url.get("data") or url.get("url")
    except Exception:
        url = r.text.strip().strip('"')
    arq = httpx.get(url, timeout=300, follow_redirects=True)
    arq.raise_for_status()
    return arq.content


# =====================================================================
#  3) ler o Excel do Leankeep e classificar (regra da aba BASE)
# =====================================================================
def _norm(t) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(t or "").upper().strip())
                   if unicodedata.category(c) != "Mn")


def _data(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    for f in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:len(datetime.now().strftime(f))], f).date()
        except Exception:
            pass
    return None


def _hora(v):
    if isinstance(v, timedelta):
        s = int(v.total_seconds()) % 86400
        return time(s // 3600, (s % 3600) // 60)
    if isinstance(v, time):
        return v
    if isinstance(v, datetime):
        return v.time()
    if isinstance(v, (int, float)):  # fração do dia (Excel)
        s = int(round(float(v) * 86400)) % 86400
        return time(s // 3600, (s % 3600) // 60)
    s = str(v or "").strip()
    for f in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, f).time()
        except Exception:
            pass
    return None


CAMPOS = {"NUMERO DA ATIVIDADE": "numero", "UNIDADE DE MANUTENCAO": "unidade", "STATUS": "status",
          "DATA PREVISTA": "data_prev", "HORA PREVISTA": "hora_prev", "DATA REALIZADA": "data_real",
          "HORA REALIZADA": "hora_real", "EXECUTOR": "executor", "PLANO": "plano", "DESCRICAO": "descricao",
          "OBSERVACAO": "observacao", "JUSTIFICATIVA": "justificativa", "UNIDADE DE MEDICAO": "un_medicao",
          "VALOR MEDIDO": "valor_medido", "FOTO": "foto"}


def ler_export(conteudo: bytes) -> tuple[list[dict], str]:
    """Linhas do Excel do Leankeep (cabeçalho na linha de 'NÚMERO DA ATIVIDADE'). Devolve (linhas, período)."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    cab, periodo, linhas = None, "", []
    for r in ws.iter_rows(values_only=True):
        if cab is None:
            for v in r:
                if isinstance(v, str) and v.strip().upper().startswith("PERÍODO"):
                    periodo = v.split(":", 1)[-1].strip()
            if any(_norm(v) == "NUMERO DA ATIVIDADE" for v in r):
                cab = {i: CAMPOS[_norm(v)] for i, v in enumerate(r) if _norm(v) in CAMPOS}
            continue
        if not r or r[0] in (None, ""):
            continue
        linhas.append({k: r[i] if i < len(r) else None for i, k in cab.items()})
    if cab is None:
        raise RuntimeError("o arquivo do Leankeep não tem a coluna 'NÚMERO DA ATIVIDADE'")
    return linhas, periodo


# Planos feitos depois do fechamento da loja: só contam como atraso se a baixa passar das 03:00 do dia seguinte
MADRUGADA = {"palavras": ["FECHAMENTO DA LOJA", "REGISTRO GAS", "EXAUST"], "ate": time(3, 0)}


def configurar(cfg: dict | None):
    """Lê do config.toml ([relatorio_semanal] prazo_madrugada / limite_madrugada)."""
    cfg = cfg or {}
    if cfg.get("prazo_madrugada") is not None:
        MADRUGADA["palavras"] = list(cfg["prazo_madrugada"])
    if cfg.get("limite_madrugada"):
        h, m = str(cfg["limite_madrugada"]).split(":")
        MADRUGADA["ate"] = time(int(h), int(m))


def de_madrugada(plano) -> bool:
    p = _norm(plano)
    return any(_norm(x) in p for x in MADRUGADA["palavras"])


def classificar(l: dict) -> str:
    """Igual à fórmula da coluna SITUAÇÃO da aba BASE (com o limite da madrugada para os fechamentos)."""
    dp, hp, dr, hr = _data(l.get("data_prev")), _hora(l.get("hora_prev")), _data(l.get("data_real")), _hora(l.get("hora_real"))
    if not dr:
        return "NÃO REALIZADO"
    if dp and dr < dp:
        return "ANTECIPADO"
    if dp:
        limite = (datetime.combine(dp + timedelta(days=1), MADRUGADA["ate"]) if de_madrugada(l.get("plano"))
                  else datetime.combine(dp, hp or time(0)))
        if datetime.combine(dr, hr or time(0)) > limite:
            return "ATRASADO"
    return "OK"


def resumir(linhas: list[dict]) -> dict:
    for l in linhas:
        l["situacao"] = classificar(l)
    st = Counter(l["situacao"] for l in linhas)
    planos = defaultdict(lambda: [0, 0, 0])   # itens, pendências, não realizados
    execs = defaultdict(lambda: [0, 0])
    for l in linhas:
        p = str(l.get("plano") or "(sem plano)")
        pend = l["situacao"] != "OK"
        planos[p][0] += 1
        planos[p][1] += pend
        planos[p][2] += l["situacao"] == "NÃO REALIZADO"
        e = str(l.get("executor") or "(sem executor)")
        execs[e][0] += 1
        execs[e][1] += pend
    return {"total": len(linhas), "status": {s: st.get(s, 0) for s in SITUACOES},
            "planos": dict(planos), "executores": dict(execs)}


# =====================================================================
#  4a) planilha (mesmo desenho da PLANILHA ANÁLISE - DIRETORES)
# =====================================================================
def planilha(linhas: list[dict], res: dict, inicio: date, fim: date, unidade: str) -> bytes:
    import openpyxl
    from openpyxl.chart import BarChart, DoughnutChart, Reference
    from openpyxl.chart.label import DataLabelList
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.table import Table, TableStyleInfo

    wb = openpyxl.Workbook()
    rs = wb.active
    rs.title = "RESUMO"
    bs = wb.create_sheet("BASE")
    A = "Arial"
    tit = Font(name=A, bold=True, size=16, color="FFFFFF")
    neg = Font(name=A, bold=True, size=10)
    nor = Font(name=A, size=10)
    fundo = PatternFill("solid", fgColor="5B1530")
    fino = Side(style="thin", color="D9D9D9")

    # ---- BASE (dados + SITUAÇÃO com a mesma fórmula da planilha original) ----
    cols = ["UNIDADE DE MANUTENÇÃO", "NÚMERO DA ATIVIDADE", "DATA PREVISTA", "HORA PREVISTA", "DATA REALIZADA",
            "HORA REALIZADA", "EXECUTOR", "PLANO", "DESCRIÇÃO", "OBSERVAÇÃO", "JUSTIFICATIVA",
            "UNIDADE DE MEDIÇÃO", "VALOR MEDIDO", "FOTO", "SITUAÇÃO"]
    bs.append(cols)
    for i, l in enumerate(linhas, start=2):
        hp, hr = _hora(l.get("hora_prev")), _hora(l.get("hora_real"))
        lim = (f"C{i}+1+TIME({MADRUGADA['ate'].hour},{MADRUGADA['ate'].minute},0)" if de_madrugada(l.get("plano"))
               else f"C{i}+D{i}")
        bs.append([l.get("unidade"), l.get("numero"), _data(l.get("data_prev")), hp, _data(l.get("data_real")), hr,
                   l.get("executor"), l.get("plano"), l.get("descricao"), l.get("observacao"), l.get("justificativa"),
                   l.get("un_medicao"), l.get("valor_medido"), l.get("foto"),
                   f'=IF(E{i}="","NÃO REALIZADO",IF(E{i}<C{i},"ANTECIPADO",IF((E{i}+F{i})>({lim}),"ATRASADO","OK")))'])
        for c, fmt in (("C", "dd/mm/yyyy"), ("E", "dd/mm/yyyy"), ("D", "hh:mm"), ("F", "hh:mm")):
            bs[f"{c}{i}"].number_format = fmt
    ult = max(2, len(linhas) + 1)
    tb = Table(displayName="BASE", ref=f"A1:O{ult}")
    tb.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    bs.add_table(tb)
    for c, w in zip("ABCDEFGHIJKLMNO", (16, 14, 12, 10, 12, 10, 20, 34, 50, 20, 20, 12, 12, 8, 16)):
        bs.column_dimensions[c].width = w
    for row in bs.iter_rows(min_row=1, max_row=ult):
        for cel in row:
            cel.font = nor
    bs.freeze_panes = "A2"
    R = f"BASE!$O$2:$O${ult}"
    H = f"BASE!$H$2:$H${ult}"
    G = f"BASE!$G$2:$G${ult}"

    # ---- RESUMO ----
    rs.merge_cells("B2:P2")
    rs["B2"] = f"{unidade} — ATIVIDADES DO LEANKEEP"
    rs["B2"].font, rs["B2"].fill = tit, fundo
    rs["B2"].alignment = Alignment(vertical="center")
    rs.row_dimensions[2].height = 30
    for r, (rot, val) in enumerate((("DATA INICIAL:", inicio), ("DATA FINAL:", fim)), start=3):
        rs[f"B{r}"], rs[f"C{r}"] = rot, val
        rs[f"B{r}"].font, rs[f"C{r}"].font = neg, nor
        rs[f"C{r}"].number_format = "dd/mm/yyyy"
    rs["B5"], rs["C5"] = "TOTAL DE DIAS:", "=C4-C3+1"
    rs["B5"].font, rs["C5"].font = neg, nor
    rs["B6"] = ("Gerado automaticamente pelo robô a partir do Relatório de Atividades do Leankeep. "
                f"Fechamentos ({', '.join(MADRUGADA['palavras']).lower()}): atraso só depois das "
                f"{MADRUGADA['ate']:%H:%M} do dia seguinte.")
    rs["B6"].font = Font(name=A, italic=True, size=9, color="808080")

    def cabec(cel, txt):
        rs[cel] = txt
        rs[cel].font = Font(name=A, bold=True, size=10, color="FFFFFF")
        rs[cel].fill = fundo

    # status
    cabec("B18", "STATUS"); cabec("C18", "QTD."); cabec("D18", "%")
    for k, s in enumerate(SITUACOES, start=19):
        rs[f"B{k}"], rs[f"C{k}"] = s, f'=COUNTIF({R},B{k})'
        rs[f"D{k}"] = f"=IFERROR(C{k}/$C$23,0)"
        rs[f"D{k}"].number_format = "0.0%"
    rs["B23"], rs["C23"], rs["D23"] = "Total", "=SUM(C19:C22)", "=IFERROR(C23/C23,0)"
    rs["D23"].number_format = "0.0%"
    for c in ("B23", "C23", "D23"):
        rs[c].font = neg

    # por plano
    ini = 25
    cabec("E24", "PLANO"); cabec("F24", "ITENS"); cabec("G24", "PENDÊNCIAS"); cabec("H24", "PESO %")
    planos = sorted(res["planos"].items(), key=lambda x: (-(x[1][1] / x[1][0] if x[1][0] else 0), -x[1][0]))
    for k, (p, _) in enumerate(planos, start=ini):
        rs[f"E{k}"] = p
        rs[f"F{k}"] = f'=COUNTIF({H},E{k})'
        rs[f"G{k}"] = f'=F{k}-COUNTIFS({R},"OK",{H},E{k})'
        rs[f"H{k}"] = f"=IFERROR(G{k}/F{k},0)"
        rs[f"H{k}"].number_format = "0.0%"
    fp = ini + len(planos)
    rs[f"E{fp}"], rs[f"F{fp}"], rs[f"G{fp}"] = "Total", f"=SUM(F{ini}:F{fp - 1})", f"=SUM(G{ini}:G{fp - 1})"
    rs[f"H{fp}"] = f"=IFERROR(G{fp}/F{fp},0)"
    rs[f"H{fp}"].number_format = "0.0%"

    # por executor
    cabec("J24", "EXECUTOR"); cabec("K24", "ITENS"); cabec("L24", "PENDÊNCIAS"); cabec("M24", "PESO %")
    execs = sorted(res["executores"].items(), key=lambda x: (-(x[1][1] / x[1][0] if x[1][0] else 0), -x[1][0]))
    for k, (e, _) in enumerate(execs, start=ini):
        rs[f"J{k}"] = e
        rs[f"K{k}"] = f'=COUNTIF({G},J{k})'
        rs[f"L{k}"] = f'=K{k}-COUNTIFS({R},"OK",{G},J{k})'
        rs[f"M{k}"] = f"=IFERROR(L{k}/K{k},0)"
        rs[f"M{k}"].number_format = "0.0%"
    fe = ini + len(execs)
    rs[f"J{fe}"], rs[f"K{fe}"], rs[f"L{fe}"] = "Total", f"=SUM(K{ini}:K{fe - 1})", f"=SUM(L{ini}:L{fe - 1})"
    rs[f"M{fe}"] = f"=IFERROR(L{fe}/K{fe},0)"
    rs[f"M{fe}"].number_format = "0.0%"

    # não realizados por plano
    cabec("O24", "PLANO"); cabec("P24", "NÃO REALIZADOS")
    nr = sorted([(p, v[2]) for p, v in res["planos"].items() if v[2]], key=lambda x: -x[1]) or [("(nenhum)", 0)]
    for k, (p, _) in enumerate(nr, start=ini):
        rs[f"O{k}"] = p
        rs[f"P{k}"] = f'=COUNTIFS({R},"NÃO REALIZADO",{H},O{k})' if p != "(nenhum)" else 0
    fn = ini + len(nr)
    rs[f"O{fn}"], rs[f"P{fn}"] = "Total", f"=SUM(P{ini}:P{fn - 1})"

    for c in (f"E{fp}", f"F{fp}", f"G{fp}", f"H{fp}", f"J{fe}", f"K{fe}", f"L{fe}", f"M{fe}", f"O{fn}", f"P{fn}"):
        rs[c].font = neg
    for row in rs.iter_rows(min_row=18, max_row=max(fp, fe, fn)):
        for cel in row:
            if cel.value is not None:
                cel.border = Border(bottom=fino)
                if cel.font is None or not cel.font.bold:
                    cel.font = nor
    for c, w in zip("ABCDEFGHIJKLMNOP", (2, 18, 12, 9, 40, 9, 12, 9, 2, 22, 9, 12, 9, 2, 40, 16)):
        rs.column_dimensions[c].width = w

    def ajustar(ch):
        ch.roundedCorners = False
        if ch.legend is not None:
            ch.legend.overlay = False
        if hasattr(ch, "x_axis"):
            ch.x_axis.delete = False
            ch.y_axis.delete = False

    d = DoughnutChart()
    d.title = "RESUMO"
    d.add_data(Reference(rs, min_col=3, min_row=18, max_row=22), titles_from_data=True)
    d.set_categories(Reference(rs, min_col=2, min_row=19, max_row=22))
    d.dataLabels = DataLabelList(showPercent=True, showVal=False, showCatName=False, showSerName=False,
                                 showLegendKey=False)
    d.height, d.width = 7.5, 9
    d.roundedCorners = False; d.legend.overlay = False
    rs.add_chart(d, "E3")

    def barras(titulo, col_cat, col_val, ini_r, fim_r, onde, pct=True):
        b = BarChart(); b.type = "bar"; b.title = titulo; b.legend = None
        b.add_data(Reference(rs, min_col=col_val, min_row=ini_r - 1, max_row=fim_r), titles_from_data=True)
        b.set_categories(Reference(rs, min_col=col_cat, min_row=ini_r, max_row=fim_r))
        b.dataLabels = DataLabelList(showVal=True, showPercent=False, showCatName=False, showSerName=False,
                                     showLegendKey=False)
        if pct:
            b.y_axis.numFmt = "0%"
        b.height, b.width = 7.5, 13
        ajustar(b)
        rs.add_chart(b, onde)

    barras("PENDÊNCIAS POR PLANOS", 5, 8, ini, fp - 1, "J3")
    barras("PENDÊNCIAS POR USUÁRIOS", 10, 13, ini, fe - 1, "E" + str(max(fp, fe, fn) + 3))
    barras("PLANOS NÃO REALIZADOS", 15, 16, ini, fn - 1, "L" + str(max(fp, fe, fn) + 3), pct=False)

    rs.page_setup.orientation = "landscape"
    rs.page_setup.fitToWidth, rs.page_setup.fitToHeight = 1, 0
    rs.sheet_properties.pageSetUpPr.fitToPage = True
    wb.calculation.fullCalcOnLoad = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =====================================================================
#  4b) imagem-resumo (mesmo estilo do quadro do cardápio)
# =====================================================================
def imagem(res: dict, inicio: date, fim: date, unidade: str, semana_ant: dict | None = None) -> bytes:
    from PIL import Image, ImageDraw
    import cardapio_imagem as CI

    W = 1600
    f_tit = CI._fonte("titulo", 44); f_sec = CI._fonte("titulo", 30)
    f_big = CI._fonte("negrito", 52); f_neg = CI._fonte("negrito", 23); f_nor = CI._fonte("normal", 22)
    f_peq = CI._fonte("normal", 19)
    cores = {"OK": (40, 160, 90), "ATRASADO": (230, 140, 30), "ANTECIPADO": (120, 110, 200), "NÃO REALIZADO": (220, 53, 69)}
    planos = sorted(res["planos"].items(), key=lambda x: (-(x[1][1] / x[1][0] if x[1][0] else 0), -x[1][0]))
    execs = sorted(res["executores"].items(), key=lambda x: (-(x[1][1] / x[1][0] if x[1][0] else 0), -x[1][0]))
    lin = 46
    alt_listas = max(len(planos), len(execs)) * lin + 90
    H = 96 + 30 + 190 + 40 + alt_listas + 70
    img = Image.new("RGB", (W, H), CI.FUNDO)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 96], fill=CI.VINHO)
    d.text((40, 48), f"Atividades Leankeep – {unidade} – {inicio:%d/%m} a {fim:%d/%m/%Y}", font=f_tit,
           fill=(255, 255, 255), anchor="lm")

    # 4 cartões de status
    tot = res["total"] or 1
    x0, y0, gap = 40, 126, 24
    lw = (W - 80 - 3 * gap) // 4
    for i, s in enumerate(SITUACOES):
        x = x0 + i * (lw + gap)
        d.rounded_rectangle([x, y0, x + lw, y0 + 170], radius=18, fill=CI.CARTAO, outline=CI.BORDA, width=2)
        d.rectangle([x, y0 + 16, x + 8, y0 + 154], fill=cores[s])
        d.text((x + 30, y0 + 30), s.title() if s != "OK" else "No prazo (OK)", font=f_neg, fill=CI.CINZA)
        n = res["status"][s]
        d.text((x + 30, y0 + 66), str(n), font=f_big, fill=CI.TEXTO)
        d.text((x + 30 + d.textlength(str(n), font=f_big) + 16, y0 + 92), f"{n / tot:.1%}".replace(".", ","),
               font=f_neg, fill=cores[s])
        if semana_ant and s in semana_ant:
            dif = n / tot - semana_ant[s]
            seta = "▲" if dif > 0.005 else ("▼" if dif < -0.005 else "=")
            d.text((x + 30, y0 + 136), f"semana anterior: {semana_ant[s]:.1%} {seta}".replace(".", ","),
                   font=f_peq, fill=CI.CINZA)

    # listas com barras: pendências por plano e por usuário
    y1 = y0 + 170 + 40
    cw = (W - 80 - gap) // 2
    for j, (titulo, itens) in enumerate((("Pendências por plano", planos), ("Pendências por usuário", execs))):
        x = 40 + j * (cw + gap)
        d.rounded_rectangle([x, y1, x + cw, y1 + alt_listas], radius=18, fill=CI.CARTAO, outline=CI.BORDA, width=2)
        d.text((x + 26, y1 + 40), titulo, font=f_sec, fill=CI.TEXTO, anchor="lm")
        d.text((x + cw - 26, y1 + 40), "pend./itens", font=f_peq, fill=CI.CINZA, anchor="rm")
        y = y1 + 82
        larg_nome = int(cw * 0.47)
        for nome, v in itens:
            itens_, pend = v[0], v[1]
            pctv = pend / itens_ if itens_ else 0
            nm = nome
            while d.textlength(nm, font=f_nor) > larg_nome - 10 and len(nm) > 4:
                nm = nm[:-2]
            if nm != nome:
                nm = nm.rstrip() + "…"
            d.text((x + 26, y + 16), nm, font=f_nor, fill=CI.TEXTO, anchor="lm")
            bx, bw = x + 26 + larg_nome, cw - 52 - larg_nome - 200
            d.rounded_rectangle([bx, y + 6, bx + bw, y + 26], radius=8, fill=(240, 236, 236))
            cor = (40, 160, 90) if pctv <= 0.05 else ((230, 140, 30) if pctv <= 0.15 else (220, 53, 69))
            if pctv > 0:
                d.rounded_rectangle([bx, y + 6, bx + max(12, int(bw * pctv)), y + 26], radius=8, fill=cor)
            d.text((x + cw - 26, y + 16), f"{pctv:.0%}  ({pend}/{itens_})", font=f_nor, fill=CI.TEXTO, anchor="rm")
            y += lin
    d.text((40, H - 34), f"Pendência = atrasada, antecipada ou não realizada. Fechamento da loja, registro de gás e exaustão: "
                         f"atraso só depois das {MADRUGADA['ate']:%H:%M} do dia seguinte. Verde até 5% · laranja até 15%.",
           font=f_peq, fill=CI.CINZA, anchor="lm")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def imagem_pizza(res: dict, inicio: date, fim: date, unidade: str) -> bytes:
    """2ª imagem: duas pizzas – situação das atividades e de quais planos vêm as pendências."""
    from PIL import Image, ImageDraw
    import cardapio_imagem as CI

    W, H = 1600, 880
    f_tit = CI._fonte("titulo", 44); f_sec = CI._fonte("titulo", 32)
    f_neg = CI._fonte("negrito", 24); f_nor = CI._fonte("normal", 22); f_peq = CI._fonte("normal", 19)
    img = Image.new("RGB", (W, H), CI.FUNDO)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 96], fill=CI.VINHO)
    d.text((40, 48), f"Atividades Leankeep – {unidade} – {inicio:%d/%m} a {fim:%d/%m/%Y}", font=f_tit,
           fill=(255, 255, 255), anchor="lm")
    cores_st = {"OK": (40, 160, 90), "ATRASADO": (230, 140, 30), "ANTECIPADO": (120, 110, 200),
                "NÃO REALIZADO": (220, 53, 69)}
    paleta = [(91, 21, 48), (230, 140, 30), (220, 53, 69), (120, 110, 200), (40, 130, 180), (150, 150, 150)]
    tot = res["total"] or 1
    fatias1 = [(("No prazo (OK)" if s == "OK" else s.title()), res["status"][s], cores_st[s]) for s in SITUACOES]
    pend = sorted(((p, v[1]) for p, v in res["planos"].items() if v[1]), key=lambda x: -x[1])
    total_pend = sum(n for _, n in pend)
    fatias2 = [(p.title(), n, paleta[i]) for i, (p, n) in enumerate(pend[:5])]
    if len(pend) > 5:
        fatias2.append(("Outros planos", sum(n for _, n in pend[5:]), paleta[5]))

    def pizza(x, titulo, fatias, total, vazio):
        cw = (W - 80 - 24) // 2
        d.rounded_rectangle([x, 126, x + cw, H - 40], radius=18, fill=CI.CARTAO, outline=CI.BORDA, width=2)
        d.text((x + 28, 170), titulo, font=f_sec, fill=CI.TEXTO, anchor="lm")
        cx, cy, r = x + cw // 2, 370, 150
        if not total:
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(235, 235, 235))
            d.text((cx, cy), vazio, font=f_neg, fill=CI.CINZA, anchor="mm")
        else:
            ang = -90.0
            for _, n, cor in fatias:
                if not n:
                    continue
                fim_ang = ang + 360.0 * n / total
                d.pieslice([cx - r, cy - r, cx + r, cy + r], ang, fim_ang, fill=cor, outline=(255, 255, 255), width=3)
                meio = (ang + fim_ang) / 2
                if n / total >= 0.06:  # % escrito dentro da fatia, quando cabe
                    import math
                    tx, ty = cx + 0.62 * r * math.cos(math.radians(meio)), cy + 0.62 * r * math.sin(math.radians(meio))
                    d.text((tx, ty), f"{n / total:.0%}", font=f_neg, fill=(255, 255, 255), anchor="mm")
                ang = fim_ang
        ly = cy + r + 50
        for nome, n, cor in fatias:
            d.rounded_rectangle([x + 28, ly - 12, x + 52, ly + 12], radius=5, fill=cor)
            nm = nome
            while d.textlength(nm, font=f_nor) > cw - 66 - 190 and len(nm) > 4:
                nm = nm[:-2]
            d.text((x + 66, ly), nm + ("…" if nm != nome else ""), font=f_nor, fill=CI.TEXTO, anchor="lm")
            d.text((x + cw - 28, ly), f"{n}  ({n / total:.1%})".replace(".", ",") if total else "0",
                   font=f_neg, fill=CI.TEXTO, anchor="rm")
            ly += 44

    pizza(40, "Situação das atividades", fatias1, tot, "sem dados")
    pizza(40 + (W - 80 - 24) // 2 + 24, f"De onde vêm as {total_pend} pendências", fatias2, total_pend,
          "nenhuma pendência")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
