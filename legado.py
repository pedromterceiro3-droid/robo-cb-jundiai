"""
Leankeep "parte antiga" (app.leankeep.com) — registrar ocorrências como o navegador faz.

O formulário de ocorrência é uma página ASP.NET antiga (EditAnomaliasPage.aspx):
  1. abre o formulário novo (GET) usando o mesmo acesso (token) do robô;
  2. lê todos os campos do formulário (inclusive os escondidos, como o __VIEWSTATE);
  3. escolher o Grupo de Áreas faz um "postback" que devolve a lista de Áreas daquele grupo;
  4. a foto sobe antes, pelo componente de upload (Telerik RadAsyncUpload);
  5. SALVAR = postback do botão Salvar com todos os campos preenchidos.

Este arquivo começa só com o TESTE (abrir, listar grupos e áreas) — sem salvar nada.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from html import unescape
from html.parser import HTMLParser

from urllib.parse import quote

import httpx

log = logging.getLogger("legado")

BASE = "https://app.leankeep.com"
# link do botão NOVO da Lista de Ocorrências (parâmetros fixos do sistema)
URL_NOVO = ("/Anomalias/EditAnomaliasPage.aspx?RedirectStyle=8h1PD8d4XrYU%2fjjuIFxkXw%3d%3d"
            "&Target=6kHYItm7D9btWhZJUrhTcNxuOK6S899b3Pq6y7jR%2b6hfrC%2bB19iGBbAJInE3KiiO082OIVAYimC"
            "%2bIBHauTK25wy30wtE9kB6N6yIgKBaXFI%3d")
P = "ctl01$PageContent$TabContainer$TabPanelAnomalia$"
CAMPO = {
    "site": P + "Site0", "grupo": P + "GrupoArea", "area": P + "AreaOcorrencia",
    "prioridade": P + "PrioridadeAnomalia", "detectado": P + "Detectado",
    "descricao": P + "Descricao", "data": P + "DataRegistro", "hora": P + "HoraRegistro",
    "limite": P + "DataLimiteExecucao",
    "emitente": P + "AnomaliasEmitentesTableControlRepeater$ctl00$Emitente",
    "foto_state": "ctl01_PageContent_TabContainer_TabPanelAnomalia_FotoAsyncUpload_ClientState",
}
BOTAO_SALVAR = "ctl01$PageContent$SaveButton$_Button"
PRIORIDADES = {"Alta": "1815", "Média": "1814", "Baixa": "1813"}

# ---- auditorias (batidão) ----
URL_LISTA_AUD = "/AuditoriasManutencao/ShowAuditoriasManutencaoPage.aspx"
URL_NOVA_AUD = ("/AuditoriasManutencao/EditAuditoriaManutencaoPage.aspx?RedirectStyle=8h1PD8d4XrYU%2fjjuIFxkXw%3d%3d"
                "&Target=6kHYItm7D9btWhZJUrhTcNXXPsKN4GBneGI4PFUxxxUbNn1%2bBMpYnsIpbZNOqKk8gP2AOaqefe0fsetX5pAu2mLDM"
                "%2b3iDq%2fY5q%2f68q8WqAI%3d")
PA = "ctl01$PageContent$TabContainer$TabPanelDadosGerais$"
CAMPO_AUD = {"site": PA + "Site0", "nome": PA + "Nome", "responsavel": PA + "Responsavel",
             "data": PA + "DataVistoria", "status": PA + "Status3", "comentarios": PA + "Comentarios"}


class _Form(HTMLParser):
    """Lê os campos do <form> (input/select/textarea) como o navegador mandaria."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.action = ""
        self.campos: dict[str, str] = {}
        self._select = None
        self._select_primeiro = None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and not self.action:
            self.action = unescape(a.get("action", ""))
        elif tag == "input":
            nome, tipo = a.get("name"), (a.get("type") or "text").lower()
            if not nome or tipo in ("submit", "button", "image", "file", "reset"):
                return
            if tipo in ("checkbox", "radio") and "checked" not in a:
                return
            self.campos[nome] = a.get("value", "on" if tipo in ("checkbox", "radio") else "")
        elif tag == "select":
            self._select = a.get("name")
            self._select_primeiro = None
        elif tag == "option" and self._select:
            v = a.get("value", "")
            if self._select_primeiro is None:
                self._select_primeiro = v
                self.campos.setdefault(self._select, v)
            if "selected" in a:
                self.campos[self._select] = v
        elif tag == "textarea":
            self._textarea = a.get("name")
            if self._textarea:
                self.campos[self._textarea] = ""

    def handle_data(self, data):
        if self._textarea:
            self.campos[self._textarea] += data

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None


def _ler_form(html: str) -> tuple[str, dict]:
    f = _Form()
    f.feed(html)
    return f.action, f.campos


def _seletor_url(html: str, pasta: str) -> str | None:
    """Link do seletor (ex.: Areas/Areas-QuickSelector.aspx?...) que a própria página monta."""
    # "Areas/Areas-QuickSelector" — sem pegar o "GruposAreas/GruposAreas-QuickSelector" por engano
    m = re.search(r"((?:\.\./)?(?<![A-Za-z])" + pasta + "/" + pasta + r"-QuickSelector\.aspx\?.*?)(?:&#39;|'|\"|\s)", html)
    if not m:
        return None
    u = unescape(m.group(1)).replace("\\u0026", "&")
    return "/" + u.lstrip("./")


def _itens_seletor(html: str) -> list[tuple[str, str]]:
    itens = re.findall(r"selectorMultiSelectRowClick\(this,\s*(?:&#39;|')[^'&]+(?:&#39;|'),\s*(?:&#39;|')(\d+)"
                       r"(?:&#39;|'),\s*(?:&#39;|')(.*?)(?:&#39;|'),", html)
    return [(i, unescape(t)) for i, t in itens]


def valor_qs(id_: str, texto: str) -> str:
    return json.dumps({"List": [{"Value": str(id_), "Text": texto}]}, ensure_ascii=False, separators=(",", ":"))


class Legado:
    def __init__(self, token: str, refresh: str | None = None, empresa_id: str = "3802",
                 url_novo: str = URL_NOVO, marca: tuple[str, str] = ("TabPanelAnomalia", "Descricao"),
                 campo: dict | None = None):
        self.url_novo = url_novo
        self.marca = marca          # (aba, campo) que provam que o formulário certo voltou
        self.campo = campo or CAMPO
        self.http = httpx.Client(base_url=BASE, timeout=httpx.Timeout(60, connect=20), follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36",
                                          "Accept-Language": "pt-BR,pt;q=0.9"})
        for dom in ("app.leankeep.com", ".leankeep.com"):
            self.http.cookies.set("authToken", token, domain=dom, path="/")
            if refresh:
                self.http.cookies.set("refreshToken", refresh, domain=dom, path="/")
            # o site novo grava estes cookies para a parte antiga saber a empresa
            self.http.cookies.set("companies", json.dumps({"id": int(empresa_id), "name": "Coco Bambu",
                                                           "tipoNome": "Building"}, separators=(",", ":")),
                                  domain=dom, path="/")
        self.pre = "ctl01"
        self.modo_escolha = None
        self.ultimo = ""
        self.ult_areas = {}
        self.html = ""
        self.action = ""
        self.campos: dict[str, str] = {}

    # ---------- formulário novo ----------
    def abrir(self):
        from zoneinfo import ZoneInfo
        agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
        url = self.url_novo
        if "/Anomalias/" in url:
            url += f"&currentDate={agora:%Y-%m-%d}&currentTime={agora:%H-%M}"
        r = self.http.get(url)
        final = str(r.url)
        if r.status_code >= 400 or re.search(r"SignIn|/login|/logout|Reauth", final, re.I):
            raise RuntimeError(f"o Leankeep antigo não aceitou o acesso do robô (foi para {final[:80]}, {r.status_code})")
        m = re.search(r'(ctl0\d)[$_]PageContent[$_](?:TabContainer[$_])?'
                      + (self.marca[0] + r'[$_]' if self.marca[0] else '') + self.marca[1], r.text)
        if m:  # a página aberta direto (fora da janelinha) usa ctl00 em vez de ctl01
            self.pre = m.group(1)
        if not m:
            titulo = re.search(r"<title>\s*(.*?)\s*</title>", r.text, re.S | re.I)
            corpo = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", r.text, flags=re.S | re.I)
            corpo = " ".join(unescape(corpo).split())
            for marca in ("Continue de onde parou", "Acesso exclusivo", "identificar sua sess", "Senha", "Login"):
                if marca.lower() in corpo.lower():
                    corpo = corpo[max(0, corpo.lower().find(marca.lower()) - 60):]
                    break
            raise RuntimeError(f"abri a página, mas não achei o formulário ({self.marca[0]}). "
                               f"Endereço: {final[:90]} | status {r.status_code} | título: "
                               f"{(titulo.group(1) if titulo else '?')[:60]} | texto: {corpo[:220]}")
        self._atualizar(r.text, final)
        return self

    def c(self, chave: str) -> str:
        """Nome do campo no formulário (com o prefixo ctl00/ctl01 que a página usou)."""
        return self.campo[chave].replace("ctl01", self.pre, 1)

    def _atualizar(self, html: str, url_atual: str):
        self.html = html
        action, campos = _ler_form(html)
        if action:
            base = url_atual.split("?")[0].rsplit("/", 1)[0] + "/"
            self.action = action if action.startswith("http") else (
                BASE + action if action.startswith("/") else base + action.lstrip("./"))
        self.campos = campos

    def _postback(self, alvo: str, extra: dict | None = None) -> str:
        dados = dict(self.campos)
        dados.update(extra or {})
        dados["__EVENTTARGET"] = alvo
        dados["__EVENTARGUMENT"] = ""
        # cabeçalho só aceita ASCII: o endereço vai codificado (antes dava "'ascii' codec can't encode")
        r = self.http.post(self.action, data=dados,
                           headers={"Referer": quote(self.action, safe=":/?&=%#;+,@!$'()*~.-_")})
        r.raise_for_status()
        if self.marca[0] not in r.text or self.marca[1] not in r.text:
            corpo = " ".join(unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", r.text,
                                             flags=re.S | re.I)).split())
            raise RuntimeError(f"depois de escolher ({alvo.rsplit('$', 2)[-2]}), o Leankeep não devolveu o "
                               f"formulário: {str(r.url)[:70]} | {corpo[:200]}")
        self._atualizar(r.text, str(r.url))
        self.ultimo = r.text
        return r.text

    def _postback_async(self, alvo: str, extra: dict | None = None, quem: str | None = None):
        """Postback "AJAX" (UpdatePanel), igual ao navegador ao fechar a janelinha de seleção."""
        dados = dict(self.campos)
        dados.update(extra or {})
        dados[f"{self.pre}$scriptManager1"] = f"{self.pre}$PageContent$UpdatePanel1|{quem or alvo}"
        dados["__EVENTTARGET"] = alvo
        dados["__EVENTARGUMENT"] = ""
        dados["__ASYNCPOST"] = "true"
        r = self.http.post(self.action, data=dados, headers={
            "Referer": quote(self.action, safe=":/?&=%#;+,@!$'()*~.-_"),
            "X-MicrosoftAjax": "Delta=true", "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
        r.raise_for_status()
        txt = r.text
        if not re.match(r"^\d+\|", txt):  # não veio no formato AJAX: tenta como página inteira
            if self.marca[1] in txt:
                self._atualizar(txt, str(r.url))
            return
        # formato: tamanho|tipo|id|conteúdo|
        i, paineis, ocultos = 0, [], {}
        while i < len(txt):
            j = txt.index("|", i); n = int(txt[i:j])
            k = txt.index("|", j + 1); tipo = txt[j + 1:k]
            m = txt.index("|", k + 1); ident = txt[k + 1:m]
            conteudo = txt[m + 1:m + 1 + n]
            i = m + 1 + n + 1
            if tipo == "updatePanel":
                paineis.append(conteudo)
            elif tipo == "hiddenField":
                ocultos[ident] = conteudo
            elif tipo == "error" or tipo == "pageRedirect":
                raise RuntimeError(f"o Leankeep respondeu {tipo}: {unescape(conteudo)[:150]}")
        for painel in paineis:
            _, novos = _ler_form(painel)
            self.campos.update(novos)
        self.campos.update(ocultos)
        if paineis:
            self.html = "\n".join(paineis) + self.html
        self.ultimo = "\n".join(paineis) if paineis else txt

    def _escolher(self, chave: str, vid: str, nome: str, conferir) -> bool:
        """Tenta os jeitos possíveis de avisar o servidor da escolha; para no que funcionar."""
        c = self.c(chave)
        extra = {c + "$Value": valor_qs(vid, nome), c + "$Text": nome}
        for modo, alvo in (("async", c.replace("$", "_")), ("async", c), ("async", c + "$Link"),
                           ("cheio", c.replace("$", "_")), ("cheio", c + "$Link")):
            try:
                (self._postback_async if modo == "async" else self._postback)(alvo, extra)
            except Exception as e:
                log.info("Leankeep antigo: escolha %s via %s/%s falhou: %s", chave, modo, alvo, e)
                continue
            if conferir():
                self.modo_escolha = (modo, alvo.replace(c, "{c}"))
                return True
        return False

    # ---------- listas ----------
    def unidade(self) -> tuple[str, str] | None:
        try:
            v = json.loads(self.campos.get(self.c("site") + "$Value", "{}"))["List"][0]
            return v["Value"], v["Text"]
        except Exception:
            return None

    def grupos(self) -> list[tuple[str, str]]:
        u = _seletor_url(self.html, "GruposAreas")
        if not u:
            raise RuntimeError("não achei o seletor de Grupo de Áreas")
        return _itens_seletor(self.http.get(u).text)

    def escolher_unidade(self, sid: str, nome: str):
        """Fixa a unidade (CB JUNDIAÍ): sem isso o seletor traz grupos de todas as lojas."""
        atual = self.unidade()
        if atual and atual[0] == str(sid):
            return
        s = self.c("site")
        ok = self._escolher("site", sid, nome, lambda: (self.unidade() or ("",))[0] == str(sid)
                            and _seletor_url(self.html, "GruposAreas") and "SiteID=" in (_seletor_url(self.html, "GruposAreas") or ""))
        if not ok:
            nomes_site = [k for k in self.campos if "Site0" in k][:4]
            raise RuntimeError(f"não consegui escolher a unidade {nome} (ficou {self.unidade()}). "
                               f"prefixo={self.pre} | campos={nomes_site} | 57896 na resposta={'57896' in self.html} | "
                               f"erro na página={re.findall(r'(?i)(erro[^<]{0,80}|invalid[^<]{0,80})', self.html)[:2]}")

    def escolher_grupo(self, gid: str, nome: str):
        antes = _seletor_url(self.html, "Areas") or ""
        return self._escolher("grupo", gid, nome, lambda: "GrupoAreaSelectID=" in (_seletor_url(self.html, "Areas") or "")
                              and (_seletor_url(self.html, "Areas") or "") != antes)

    def areas(self) -> list[tuple[str, str]]:
        u = _seletor_url(self.html, "Areas")
        if not u:
            raise RuntimeError("não achei o seletor de Áreas")
        r = self.http.get(u)
        itens = _itens_seletor(r.text)
        self.ult_areas = {"url_tem_grupo": "GrupoAreaSelectID=" in u, "status": r.status_code, "tam": len(r.text),
                          "final": str(r.url)[:60], "linhas": r.text.count("selectorMultiSelectRowClick"),
                          "titulo": (re.search(r"<title>\s*(.*?)\s*</title>", r.text, re.S) or [None, "?"])[1][:40]}
        return itens


    # ---------- preencher e salvar ----------
    def escolher_area(self, aid: str, nome: str) -> bool:
        a = self.c("area")
        return self._escolher("area", aid, nome, lambda: aid in self.campos.get(a + "$Value", ""))

    def enviar_foto(self, conteudo: bytes, nome_arquivo: str = "foto.jpg", tipo: str = "image/jpeg"):
        """Sobe a foto pelo componente de upload do formulário (Telerik RadAsyncUpload)."""
        cfgm = re.search(r'"_serializedConfiguration"\s*:\s*"([^"]+)"', self.html)
        if not cfgm:
            raise RuntimeError("não achei o componente de foto no formulário")
        config = json.loads('"' + cfgm.group(1) + '"')  # desfaz os escapes do JSON (\/, \u002b…)
        tipom = re.search(r'"_serializedConfigurationType"\s*:\s*"([^"]+)"', self.html)
        config_tipo = json.loads('"' + tipom.group(1) + '"') if tipom else ""
        handler = re.search(r'"_handlerUrl"\s*:\s*"([^"]+)"', self.html)
        url_up = json.loads('"' + handler.group(1) + '"') if handler else "/Telerik.Web.UI.WebResource.axd?type=rau"
        # igual ao site (RadAsyncUpload): rauPostData = configuração & tipo; "metadata" vai à parte
        meta = {"TotalChunks": 1, "ChunkIndex": 0, "TotalFileSize": len(conteudo),
                "UploadID": f"{int(datetime.now().timestamp() * 1000)}{nome_arquivo}", "IsSingleChunkUpload": True}
        meta_txt = json.dumps(meta, separators=(",", ":"))
        r = self.http.post(url_up if url_up.startswith("http") else BASE + url_up,
                           data={"rauPostData": f"{config}&{config_tipo}", "fileName": nome_arquivo, "contentType": tipo,
                                 "lastModifiedDate": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                 "metadata": meta_txt},
                           files={"file": (nome_arquivo, conteudo, tipo)},
                           headers={"Referer": quote(self.action, safe=":/?&=%#;+,@!$'()*~.-_")})
        try:
            j = r.json()
        except Exception:
            corpo = " ".join(unescape(re.sub(r"<[^>]+>", " ", r.text)).split())
            raise RuntimeError(f"upload da foto respondeu {r.status_code}: {corpo[:160] or '(vazio)'}")
        if "fileInfo" not in j:
            raise RuntimeError(f"o upload da foto não foi aceito: {r.text[:150]}")
        estado = {"isEnabled": "true", "uploadedFiles": [{"fileInfo": j["fileInfo"], "metaData": j["metaData"]}]}
        self.campos[self.c("foto_state")] = json.dumps(estado, separators=(",", ":"))

    def _linhas_emitente(self, html: str) -> list[str]:
        """Nomes (sem $Value) das linhas de Solicitante que existem no formulário devolvido."""
        return list(dict.fromkeys(re.findall(
            r'name="([^"]*AnomaliasEmitentesTableControlRepeater\$ctl\d+\$Emitente)\$Value"', html)))

    def garantir_emitente(self, emitente: tuple[str, str] = ("84106", "Pedro Melo")) -> bool:
        """Solicitante é obrigatório. Ao trocar a unidade o formulário apaga a linha: clica no ⊕ e preenche."""
        eid, enome = emitente
        base = self.ultimo or self.html
        linhas = self._linhas_emitente(base)
        if not linhas:  # clica no ⊕ (botão-imagem: o navegador manda nome.x / nome.y)
            botao = f"{self.pre}$PageContent$TabContainer$TabPanelAnomalia$EmitenteAddButton"
            for modo in ("async", "cheio"):
                try:
                    if modo == "async":
                        self._postback_async("", {botao + ".x": "6", botao + ".y": "6"}, quem=botao)
                    else:
                        self._postback("", {botao + ".x": "6", botao + ".y": "6"})
                except Exception as e:
                    log.info("Leankeep antigo: ⊕ solicitante via %s falhou: %s", modo, e)
                    continue
                linhas = self._linhas_emitente(self.ultimo or self.html)
                if linhas:
                    break
        if not linhas:
            return False
        nome = linhas[0]
        extra = {nome + "$Value": valor_qs(eid, enome), nome + "$Text": enome}
        self.campos.update(extra)
        # avisa o servidor da escolha, como a janelinha faz (mesmo caminho da unidade/grupo)
        for alvo in (nome.replace("$", "_"), nome):
            try:
                self._postback_async(alvo, extra)
                break
            except Exception:
                continue
        self.campos.update(extra)
        return True

    def preencher(self, descricao: str, prioridade: str, quando: datetime | None = None,
                  emitente: tuple[str, str] = ("84106", "Pedro Melo")):
        q = quando or datetime.now()
        e = self.c("emitente")
        atual = self.campos.get(e + "$Value", "")
        if not re.search(r'"Value":"\d+"', atual):
            self.campos[e + "$Value"] = valor_qs(*emitente)
            self.campos[e + "$Text"] = emitente[1]
        self.campos[self.c("descricao")] = descricao[:3900]
        self.campos[self.c("prioridade")] = PRIORIDADES.get(prioridade, prioridade)
        self.campos[self.c("data")] = q.strftime("%d/%m/%Y")
        self.campos[self.c("hora")] = q.strftime("%H:%M")

    def salvar(self, descricao: str = "") -> str:
        """Clica em SALVAR. Devolve o número da ocorrência (ex.: D171399)."""
        alvo = f"{self.pre}$PageContent$SaveButton$_Button"
        dados = dict(self.campos)
        dados["__EVENTTARGET"], dados["__EVENTARGUMENT"] = alvo, ""
        r = self.http.post(self.action, data=dados, headers={"Referer": quote(self.action, safe=":/?&=%#;+,@!$'()*~.-_")})
        r.raise_for_status()
        texto = " ".join(unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", r.text,
                                         flags=re.S | re.I)).split())
        m = re.search(r"N[ºo°]\s*Ocorr[eê]ncia\s*([A-Z]\d{5,7})", texto)
        if m:
            return m.group(1)
        # depois de salvar, o Leankeep pode voltar para a LISTA: acha a linha com a nossa descrição
        trecho = " ".join((descricao.strip().splitlines() or [""])[0].split())[:30]
        if trecho:
            i = texto.find(trecho)
            if i >= 0:
                antes = re.findall(r"\b([A-Z]\d{6})\b", texto[max(0, i - 400):i])
                if antes:
                    return antes[-1]
                depois = re.search(r"\b([A-Z]\d{6})\b", texto[i:i + 200])
                if depois:
                    return depois.group(1)
        # (só conta como salva quando o número aparece — nada de "provavelmente salvou")
        # não salvou: junta os avisos que o site mostraria (alertas, validações) e o estado dos campos
        alertas = re.findall(r"""alert\(\s*['"]([^'"]{3,200})['"]""", r.text)
        alertas += re.findall(r"""(?:ShowMessage|showMessage|MessageBox|radalert)\(\s*['"]([^'"]{3,200})['"]""", r.text)
        valid = re.findall(r'(?is)<span[^>]+(?:Validator|error|erro)[^>]*>\s*([^<]{3,150})</span>', r.text)
        _, campos = _ler_form(r.text) if "TabPanelAnomalia" in r.text else ("", {})
        def val(ch):
            v = campos.get(self.c(ch) + "$Value") or campos.get(self.c(ch), "")
            return (v[:60] if v else "vazio")
        estado = (f"unidade={val('site')} | grupo={val('grupo')} | área={val('area')} | "
                  f"prioridade={campos.get(self.c('prioridade'), '?')} | descrição={'ok' if campos.get(self.c('descricao')) else 'vazia'}"
                  ) if campos else "formulário não voltou"
        raise RuntimeError("o Leankeep não salvou. "
                           + (f"Avisos: {' | '.join(dict.fromkeys(alertas + valid))[:300]}. " if alertas or valid else "")
                           + f"Campos: {estado}. Página: {str(r.url)[-60:]}")

    # ---------- auditorias (batidão) ----------
    def _get(self, url: str):
        r = self.http.get(url)
        final = str(r.url)
        if r.status_code >= 400 or re.search(r"SignIn|/login|/logout|Reauth", final, re.I):
            raise RuntimeError(f"o Leankeep antigo não aceitou o acesso do robô (foi para {final[:80]}, {r.status_code})")
        return r

    def _enviar_form(self, url_atual: str, html: str, extra: dict) -> httpx.Response:
        action, campos = _ler_form(html)
        base = url_atual.split("?")[0].rsplit("/", 1)[0] + "/"
        alvo = action if action.startswith("http") else (BASE + action if action.startswith("/")
                                                          else base + action.lstrip("./"))
        campos["__EVENTTARGET"], campos["__EVENTARGUMENT"] = "", ""
        campos.update(extra)
        r = self.http.post(alvo, data=campos, headers={"Referer": quote(url_atual, safe=":/?&=%#;+,@!$'()*~.-_")})
        r.raise_for_status()
        return r

    def auditorias(self, busca: str = "JUNDIA") -> list[dict]:
        """Últimas auditorias da unidade (lista do site, filtro 'Tudo'), da mais antiga para a mais nova."""
        r = self._get(URL_LISTA_AUD)
        pc = "ctl00$PageContent$"
        r = self._enviar_form(str(r.url), r.text, {pc + "AuditoriasManutencaoNListSearchText1": busca,
                                                    pc + "StatusFilter": "--ANY--",
                                                    pc + "SearchButton2.x": "5", pc + "SearchButton2.y": "5"})
        r = self._enviar_form(str(r.url), r.text, {pc + "Pagination$_LastPage.x": "3", pc + "Pagination$_LastPage.y": "3"})
        linhas = _linhas_aud(r.text)
        if len(linhas) < 6:  # última página quase vazia: pega também a anterior
            r2 = self._enviar_form(str(r.url), r.text, {pc + "Pagination$_PreviousPage.x": "3",
                                                         pc + "Pagination$_PreviousPage.y": "3"})
            linhas = _linhas_aud(r2.text) + linhas
        vistos, out = set(), []
        for a in linhas:
            if a["id"] not in vistos:
                vistos.add(a["id"])
                out.append(a)
        return sorted(out, key=lambda a: int(a["id"]))

    def url_ocorrencia_na_auditoria(self, url_aud: str) -> str:
        """Abre a auditoria e clica em Ocorrências › Novo: devolve o endereço do formulário já ligado a ela."""
        r = self._get(url_aud)
        m = re.search(r"(ctl0\d)\$PageContent\$TabContainer\$TabPanelAnomalias\$NewButton", r.text)
        if not m:
            raise RuntimeError("abri a auditoria, mas não achei a aba Ocorrências › Novo")
        r2 = self._enviar_form(str(r.url), r.text,
                               {"__EVENTTARGET": f"{m.group(1)}$PageContent$TabContainer$TabPanelAnomalias$NewButton$_Button"})
        m2 = re.search(r"Anomalias/(EditAnomaliasPage\.aspx\?[^'\"\s<]*?AuditoriaManutencaoID=[^'\"\s<&]+)", r2.text)
        if not m2:
            raise RuntimeError("cliquei em Novo dentro da auditoria, mas o Leankeep não devolveu o formulário ligado")
        return "/Anomalias/" + unescape(m2.group(1))


UNIDADE = ("57896", "CB JUNDIAÍ")


def registrar(token: str, refresh: str | None, grupo: tuple[str, str], area: tuple[str, str], prioridade: str,
              descricao: str, foto: bytes | None = None, quando: datetime | None = None,
              empresa_id: str = "3802", unidade: tuple[str, str] = UNIDADE,
              emitente: tuple[str, str] = ("84106", "Pedro Melo"), auditoria: dict | None = None) -> str:
    """Cria a ocorrência no Leankeep (como o formulário do site). Devolve o número.
    Com `auditoria` (um item de auditorias()), a ocorrência nasce DENTRO dela (aba Ocorrências › Novo)."""
    url_novo = URL_NOVO
    if auditoria:
        url_novo = Legado(token, refresh, empresa_id).url_ocorrencia_na_auditoria(auditoria["url"])
    lg = Legado(token, refresh, empresa_id, url_novo=url_novo).abrir()
    lg.escolher_unidade(*unidade)
    if not lg.escolher_grupo(*grupo):
        raise RuntimeError(f"o Leankeep não aceitou o grupo {grupo[1]}")
    if not lg.escolher_area(*area):
        raise RuntimeError(f"o Leankeep não aceitou a área {area[1]}")
    if not lg.garantir_emitente(emitente):
        log.warning("Leankeep antigo: não consegui incluir a linha do Solicitante")
    lg.preencher(descricao, prioridade, quando, emitente)
    aviso_foto = ""
    if foto:
        try:
            lg.enviar_foto(foto)
        except Exception as e:  # sem a foto, registra mesmo assim e avisa
            log.warning("Leankeep: foto não subiu: %s", e)
            aviso_foto = f"sem foto ({e})"
    numero = lg.salvar(descricao)
    return f"{numero}|{aviso_foto}" if aviso_foto else numero


def testar(token: str, refresh: str | None = None, max_grupos: int = 20, empresa_id: str = "3802",
           unidade: tuple[str, str] = UNIDADE) -> dict:
    """Só leitura: abre o formulário novo, lista os grupos e as áreas de cada grupo. NÃO salva."""
    lg = Legado(token, refresh, empresa_id).abrir()
    lg.escolher_unidade(*unidade)
    res = {"unidade": lg.unidade(), "emitente": lg.campos.get(lg.c("emitente") + "$Text"), "prefixo": lg.pre,
           "modo": lg.modo_escolha,
           "prioridades": PRIORIDADES, "grupos": {}}
    for gid, nome in lg.grupos()[:max_grupos]:
        try:
            ok = lg.escolher_grupo(gid, nome)
            areas = lg.areas()
            res["grupos"][f"{gid}|{nome}"] = areas if areas else (
                f"0 áreas (grupo aceito={ok}, modo={lg.modo_escolha}, diagnóstico={getattr(lg, 'ult_areas', {})})")
        except Exception as e:
            res["grupos"][f"{gid}|{nome}"] = f"erro: {e}"
    return res


# =====================================================================
#  Auditorias (batidão): "Auditoria N" – N conta dentro do mês, sem repetir
# =====================================================================
def _limpo(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _linhas_aud(html: str) -> list[dict]:
    """Linhas da lista de auditorias: número, unidade, nome, data e o link de edição."""
    out = []
    for m in re.finditer(r"EditAuditoriaManutencaoPage\.aspx\?(AuditoriasManutencao=.*?)&#39;(.*?)</tr>", html, re.S):
        cel = [_limpo(c) for c in re.findall(r'<td class="tableCellValue">(.*?)</td>', m.group(2), re.S)]
        if len(cel) >= 4 and cel[0].isdigit():
            try:
                data = datetime.strptime(cel[3], "%d/%m/%Y").date()
            except ValueError:
                continue
            out.append({"id": cel[0], "unidade": cel[1], "nome": cel[2], "data": data.isoformat(),
                        "url": "/AuditoriasManutencao/EditAuditoriaManutencaoPage.aspx?" + unescape(m.group(1))})
    return out


def _num_auditoria(nome: str) -> int | None:
    m = re.fullmatch(r"\s*auditoria\s*(\d+)\s*", nome or "", re.I)
    return int(m.group(1)) if m else None


def situacao_auditoria(lista: list[dict], hoje, dias_semana: int = 6) -> dict:
    """atual = auditoria 'Auditoria N' mais nova dos últimos 7 dias; proximo = próximo nome livre do mês."""
    from datetime import date as _d, timedelta
    hoje = hoje if isinstance(hoje, _d) else hoje.date()
    numeradas = [a for a in lista if _num_auditoria(a["nome"]) is not None]
    recentes = [a for a in numeradas if hoje - timedelta(days=dias_semana) <= _d.fromisoformat(a["data"]) <= hoje]
    do_mes = [_num_auditoria(a["nome"]) for a in numeradas if a["data"][:7] == hoje.isoformat()[:7]]
    return {"atual": recentes[-1] if recentes else None, "proximo": f"Auditoria {max(do_mes, default=0) + 1}",
            "mes": [a for a in numeradas if a["data"][:7] == hoje.isoformat()[:7]]}


def listar_auditorias(token: str, refresh: str | None = None, empresa_id: str = "3802") -> list[dict]:
    return Legado(token, refresh, empresa_id).auditorias()


def criar_auditoria(token: str, refresh: str | None, nome: str, data, comentarios: str = "",
                    empresa_id: str = "3802", unidade: tuple[str, str] = UNIDADE,
                    responsavel: tuple[str, str] = ("84106", "Pedro Melo")) -> dict:
    """Cria a auditoria (Dados Gerais) e devolve a linha dela na lista ({id, nome, data, url}).
    Se já existir uma com o mesmo nome no mês, devolve essa (nunca duplica)."""
    base = Legado(token, refresh, empresa_id)
    mes = data.strftime("%Y-%m")
    for a in base.auditorias():
        if a["nome"].strip().lower() == nome.strip().lower() and a["data"][:7] == mes:
            a["ja_existia"] = True
            return a
    lg = Legado(token, refresh, empresa_id, url_novo=URL_NOVA_AUD, marca=("TabPanelDadosGerais", "Nome"),
                campo=CAMPO_AUD).abrir()
    sid, snome = unidade
    if (lg.unidade() or ("",))[0] != sid:
        if not lg._escolher("site", sid, snome, lambda: (lg.unidade() or ("",))[0] == sid):
            log.info("Leankeep antigo: unidade da auditoria preenchida direto (sem postback)")
    c = lg.c
    lg.campos[c("site") + "$Value"], lg.campos[c("site") + "$Text"] = valor_qs(sid, snome), snome
    lg.campos[c("responsavel") + "$Value"] = valor_qs(*responsavel)
    lg.campos[c("responsavel") + "$Text"] = responsavel[1]
    lg.campos[c("nome")] = nome
    lg.campos[c("data")] = data.strftime("%d/%m/%Y")
    lg.campos[c("status")] = "1"  # Pendente
    lg.campos[c("comentarios")] = comentarios[:1000]
    dados = dict(lg.campos)
    dados["__EVENTTARGET"], dados["__EVENTARGUMENT"] = f"{lg.pre}$PageContent$SaveButton$_Button", ""
    r = lg.http.post(lg.action, data=dados, headers={"Referer": quote(lg.action, safe=":/?&=%#;+,@!$'()*~.-_")})
    r.raise_for_status()
    # confere na lista (só conta como criada quando aparece lá)
    for a in reversed(base.auditorias()):
        if a["nome"].strip().lower() == nome.strip().lower() and a["data"] == data.strftime("%Y-%m-%d"):
            return a
    alertas = re.findall(r"""alert\(\s*['"]([^'"]{3,200})['"]""", r.text)
    valid = re.findall(r'(?is)<span[^>]+(?:Validator|error|erro)[^>]*>\s*([^<]{3,150})</span>', r.text)
    raise RuntimeError("o Leankeep não salvou a auditoria. "
                       + (f"Avisos: {' | '.join(dict.fromkeys(alertas + valid))[:300]}. " if alertas or valid else "")
                       + f"Página: {str(r.url)[-60:]}")


# =====================================================================
#  Seguimento da ocorrência (aba Correções › Incluir Correção)
# =====================================================================
URL_PAINEL = "/Anomalias/ShowAnomaliasTablePage.aspx"
URL_CORRECAO = ("/Anomalias/EditAcoesCorretivasPage.aspx?Anomalias={codigo}&RedirectStyle=8h1PD8d4XrYU%2fjjuIFxkXw%3d%3d"
                "&Target=6kHYItm7D9btWhZJUrhTcDunIV3yCAb5FQiBn0byUBdB7%2bni7wolCitFL9%2bNezzPSotyYrI2AsSwMgJM22LKLQ%3d%3d"
                "&TipoAcaoCorretivaID=JIpL%2b91tzcHweWzX9Iv13w%3d%3d")
STATUS_CORRECAO = {  # código → nome (lista "Status" da correção, empresa Coco Bambu)
    "35788": "Gerado O.S", "35782": "Em Andamento", "35785": "Agendada", "35795": "Equipe no local",
    "35790": "Aguardando Material", "37117": "Aguardando terceirizada", "35791": "Em Orçamento",
    "40114": "Aguardando diretoria", "35797": "Encaminhada para Assistência Técnica",
    "35796": "Atendimento Realizado", "42402": "Realizado - Diretor aprovar", "37409": "Solucionada"}
SOLUCIONADA = "37409"
PC = "ctl01$PageContent$"
CAMPO_CORR = {"status": PC + "TipoAcaoCorretivaEmpresa", "inicio": PC + "Inicio", "hora": PC + "HoraInicio",
              "descricao": PC + "Descricao", "foto_state": "ctl01_PageContent_FotoAsyncUpload_ClientState"}


def _codigo_no_html(html: str, numero: str) -> str | None:
    """Acha o link de edição da linha que tem o número (o link vem ANTES do número, na mesma linha)."""
    for m in re.finditer(re.escape(numero), html):
        trecho = html[max(0, m.start() - 4000):m.start()]
        links = list(re.finditer(r"EditAnomaliasPage\.aspx\?(Anomalias=[^'\"\s<]+?)(?:&#39;|'|\"|\s|<)", trecho))
        if links:
            return unescape(links[-1].group(1))  # "Anomalias=...&RedirectStyle=...&Target=..."
    return None


def codigo_ocorrencia(lg: "Legado", numero: str) -> str:
    """Procura a ocorrência pelo número (ex.: D171431) no Painel e devolve a query do link dela
    ("Anomalias=...&RedirectStyle=...&Target=...")."""
    numero = numero.strip().upper()
    r = lg._get(URL_PAINEL)
    pagina0 = r.text
    pc = "ctl00$PageContent$"
    tentativas = [
        {pc + "ProcurarPorFilter": "CQA", pc + "AnomaliasNlistSearchText1": numero,
         pc + "StatusFilter2": ["1", "2", "3", "4", "5"], pc + "SearchButton2.x": "4", pc + "SearchButton2.y": "4"},
        {pc + "ProcurarPorFilter": "CQA", pc + "AnomaliasNlistSearchText1": numero.lstrip("D"),
         pc + "StatusFilter2": ["1", "2", "3", "4", "5"], pc + "SearchButton2.x": "4", pc + "SearchButton2.y": "4"},
        {pc + "AnomaliasNlistSearchText1": numero, pc + "SearchButton2.x": "4", pc + "SearchButton2.y": "4"},
    ]
    diag = []
    for extra in tentativas:
        try:
            r2 = lg._enviar_form(str(r.url), pagina0, extra)
        except Exception as e:
            diag.append(f"busca falhou: {e}")
            continue
        cod = _codigo_no_html(r2.text, numero)
        if cod:
            return cod
        titulo = (re.search(r"<title>\s*(.*?)\s*</title>", r2.text, re.S) or [None, "?"])[1][:40]
        total = re.search(r"Total de\s*(\d+)\s*itens", _limpo(r2.text))
        diag.append(f"página '{titulo}', {r2.text.count('EditAnomaliasPage.aspx?Anomalias=')} links, "
                    f"total {total.group(1) if total else '?'}, número na página: {numero in r2.text}")
    cod = _codigo_no_html(pagina0, numero)  # às vezes já está na 1ª página
    if cod:
        return cod
    raise RuntimeError(f"não achei a ocorrência {numero} no Painel de Ocorrências do Leankeep "
                       f"({' | '.join(diag)[:300]})")



def url_correcao_pela_ocorrencia(lg: "Legado", query: str) -> str | None:
    """Abre a ocorrência (como no site) e clica em "Incluir Correção": devolve o endereço que o site abre."""
    try:
        r = lg._get("/Anomalias/EditAnomaliasPage.aspx?" + query)
        m = re.search(r"(ctl0\d)\$PageContent\$TabContainer\$TabPanelAnomalia\$NewButton2", r.text)
        if not m:
            log.info("Leankeep antigo: botão Incluir Correção não achado na ocorrência")
            return None
        r2 = lg._enviar_form(str(r.url), r.text, {
            "__EVENTTARGET": f"{m.group(1)}$PageContent$TabContainer$TabPanelAnomalia$NewButton2$_Button"})
        achou = re.search(r"(?:\.\./Anomalias/)?(EditAcoesCorretivasPage\.aspx\?[^'\"\s<]+?)(?:&#39;|'|\"|\s|<)", r2.text)
        if not achou:
            log.info("Leankeep antigo: Incluir Correção não devolveu o endereço (%s)",
                     (re.findall(r"alert\('([^']{0,120})", r2.text) or ["sem aviso"])[0])
            return None
        url = "/Anomalias/" + unescape(achou.group(1))
        return re.sub(r"&currentDate=[^&]*|&currentTime=[^&]*", "", url)
    except Exception as e:
        log.info("Leankeep antigo: Incluir Correção falhou: %s", e)
        return None


def _garantir_pessoa(lg: "Legado", repeater: str, campo: str, botao: str, pessoa: tuple[str, str]) -> bool:
    """Solicitante/Executor da correção: se a linha sumiu (o formulário recarrega ao trocar o status),
    clica no ⊕ e preenche com a pessoa."""
    pid, pnome = pessoa
    padrao = r'name="([^"]*' + repeater + r'\$ctl\d+\$' + campo + r')\$Value"'

    def linhas():
        return list(dict.fromkeys(re.findall(padrao, (lg.ultimo or "") + lg.html)))
    achadas = linhas()
    if not achadas:
        nome_botao = f"{lg.pre}$PageContent${botao}"
        for modo in ("async", "cheio"):
            try:
                extra = {nome_botao + ".x": "6", nome_botao + ".y": "6"}
                if modo == "async":
                    lg._postback_async("", extra, quem=nome_botao)
                else:
                    lg._postback("", extra)
            except Exception as e:
                log.info("Leankeep antigo: ⊕ %s via %s falhou: %s", campo, modo, e)
                continue
            achadas = linhas()
            if achadas:
                break
    if not achadas:
        return False
    nome = achadas[0]
    extra = {nome + "$Value": valor_qs(pid, pnome), nome + "$Text": pnome}
    lg.campos.update(extra)
    for alvo in (nome.replace("$", "_"), nome):
        try:
            lg._postback_async(alvo, extra)
            break
        except Exception:
            continue
    lg.campos.update(extra)
    return True

def seguimento(token: str, refresh: str | None, numero: str, status: str, descricao: str = "",
               foto: bytes | None = None, quando: datetime | None = None, empresa_id: str = "3802",
               pessoa: tuple[str, str] = ("84106", "Pedro Melo")) -> str:
    """Inclui uma correção (seguimento) na ocorrência. status = código de STATUS_CORRECAO.
    Devolve "ok" ou "ok|aviso"."""
    if status not in STATUS_CORRECAO:
        raise RuntimeError(f"status desconhecido: {status}")
    if status == SOLUCIONADA and not foto:
        raise RuntimeError("para Solucionada o Leankeep exige a foto da resolução")
    base = Legado(token, refresh, empresa_id)
    query = codigo_ocorrencia(base, numero)
    codigo = re.match(r"Anomalias=([^&]+)", query).group(1)
    url_corr = url_correcao_pela_ocorrencia(base, query) or URL_CORRECAO.format(codigo=codigo)
    lg = Legado(token, refresh, empresa_id, url_novo=url_corr, marca=("", "TipoAcaoCorretivaEmpresa"), campo=CAMPO_CORR)
    lg.http = base.http  # mesma sessão: o Leankeep guarda a ocorrência aberta na sessão
    lg.abrir()
    # trocar o Status recarrega o formulário (igual ao site)
    if lg.campos.get(lg.c("status")) != status:
        lg.campos[lg.c("status")] = status
        try:
            lg._postback(lg.c("status"))
        except Exception as e:
            log.info("Leankeep antigo: postback do status falhou (%s); tentando AJAX", e)
            lg._postback_async(lg.c("status"))
        lg.campos[lg.c("status")] = status
    # Solicitante e Executor são obrigatórios: trocar o status recarrega o formulário e pode apagá-los
    sem = [n for n, rep, campo, bt in (("Solicitante", "AcoesCorretivasEmitentesTableControlRepeater", "Emitente", "EmitenteAddButton"),
                                       ("Executor", "AcoesCorretivasExecutoresTableControlRepeater", "Executor", "ExecutorAddButton"))
           if not _garantir_pessoa(lg, rep, campo, bt, pessoa)]
    if sem:
        log.warning("Leankeep antigo: não consegui preencher %s na correção", ", ".join(sem))
    q = quando or datetime.now()
    lg.campos[lg.c("inicio")] = q.strftime("%d/%m/%Y")
    lg.campos[lg.c("hora")] = q.strftime("%H:%M")
    lg.campos[lg.c("descricao")] = (descricao or STATUS_CORRECAO[status])[:3900]
    if foto:
        lg.enviar_foto(foto)  # obrigatória em Solucionada: se falhar, não salva
    dados = dict(lg.campos)
    dados["__EVENTTARGET"], dados["__EVENTARGUMENT"] = f"{lg.pre}$PageContent$SaveButton$_Button", ""
    r = lg.http.post(lg.action, data=dados, headers={"Referer": quote(lg.action, safe=":/?&=%#;+,@!$'()*~.-_")})
    r.raise_for_status()
    alertas = re.findall(r"""alert\(\s*['"]([^'"]{3,200})['"]""", r.text)
    valid = [v for v in re.findall(r'(?is)<span[^>]+(?:Validator|error|erro)[^>]*>\s*([^<]{3,150})</span>', r.text)
             if v.strip()]
    if (alertas or valid) and "TipoAcaoCorretivaEmpresa" in r.text:
        raise RuntimeError("o Leankeep não salvou o seguimento. Avisos: " + " | ".join(dict.fromkeys(alertas + valid))[:300])
    # confere na própria ocorrência: a correção nova aparece na lista dela
    try:
        o = base._get("/Anomalias/EditAnomaliasPage.aspx?" + query)
        texto = _limpo(o.text)
        if STATUS_CORRECAO[status] in texto and q.strftime("%d/%m/%Y") in texto:
            return "ok"
        return "ok|não consegui confirmar na lista de correções – confira no Leankeep"
    except Exception as e:
        return f"ok|não consegui conferir ({e})"


# =====================================================================
#  Excluir ocorrência (Lista de Ocorrências › marcar a linha › Excluir)
# =====================================================================
URL_LISTA_OC = "/Anomalias/ShowListaAnomaliasPage.aspx"


def _buscar_lista(lg: "Legado", numero: str, variante: int = 0):
    r = lg._get(URL_LISTA_OC)
    pc = "ctl00$PageContent$"
    extra = {pc + "AnomaliasUltimasAcoesNListSearchText1": numero,
             pc + "SearchButton3.x": "4", pc + "SearchButton3.y": "4"}
    if variante in (0, 1):
        extra[pc + "ProcurarPorFilter1"] = "CQA"
    if variante == 0:
        extra[pc + "StatusAnomaliaFilter2"] = ["1", "2", "3", "4", "5"]
    if variante == 2:  # sem filtros de data, caso a sessão do robô venha com período
        _, campos = _ler_form(r.text)
        for k in campos:
            if re.search(r"Data\w*Filter\d*$", k):
                extra[k] = ""
        extra[pc + "ProcurarPorFilter1"] = "CQA"
        extra[pc + "StatusAnomaliaFilter2"] = ["1", "2", "3", "4", "5"]
    return lg._enviar_form(str(r.url), r.text, extra)


def _linha_da_ocorrencia(html: str, numero: str) -> list[str]:
    """Nomes das caixinhas SelectRow das linhas cujo número é exatamente `numero`."""
    achadas = []
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        caixa = re.search(r'name="([^"]*AnomaliasUltimasAcoesNListTableControlRepeater\$ctl\d+\$SelectRow)"', tr)
        if caixa and re.search(r">\s*" + re.escape(numero) + r"\s*</a>", tr):
            achadas.append(caixa.group(1))
    return achadas



def _cookies_como_navegador(lg: "Legado", empresa_id: str):
    """A Lista de Ocorrências lê os filtros globais dos cookies (iguais aos do navegador): sem eles vem vazia."""
    for dom in ("app.leankeep.com", ".leankeep.com"):
        lg.http.cookies.set("companies", json.dumps({"id": str(empresa_id), "name": "Coco Bambu"},
                                                    separators=(",", ":"), ensure_ascii=False), domain=dom, path="/")
        for nome in ("unitGroups", "unitSubgroups", "units", "states", "cities", "providers"):
            lg.http.cookies.set(nome, "", domain=dom, path="/")

def excluir(token: str, refresh: str | None, numero: str, empresa_id: str = "3802") -> str:
    """Exclui a ocorrência no Leankeep (só ela). Devolve 'ok' depois de conferir que sumiu da lista."""
    numero = numero.strip().upper()
    lg = Legado(token, refresh, empresa_id)
    _cookies_como_navegador(lg, empresa_id)
    diag, linhas, r = [], [], None
    for v in (-1, 0, 1, 2):  # -1 = a lista como abre, sem buscar (as mais recentes vêm na 1ª página)
        r = lg._get(URL_LISTA_OC) if v == -1 else _buscar_lista(lg, numero, v)
        linhas = _linha_da_ocorrencia(r.text, numero)
        if len(linhas) == 1:
            break
        titulo = (re.search(r"<title>\s*(.*?)\s*</title>", r.text, re.S) or [None, "?"])[1][:35]
        n_linhas = r.text.count('$SelectRow"')
        diag.append(f"v{v}: '{titulo}', {n_linhas} linhas, número {r.text.count(numero)}x, "
                    f"{str(r.url)[-35:]}")
    if len(linhas) != 1:
        raise RuntimeError(f"não excluí: achei {len(linhas)} linha(s) com o número {numero} na Lista de Ocorrências "
                           f"(preciso de exatamente 1). " + " | ".join(diag)[:350])
    # só a caixinha desta ocorrência marcada (o formulário não traz nenhuma outra marcada)
    action, campos = _ler_form(r.text)
    campos = {k: v for k, v in campos.items() if not k.endswith("$SelectRow") and not k.endswith("$ToggleAll")}
    campos[linhas[0]] = "on"
    campos["__EVENTTARGET"], campos["__EVENTARGUMENT"] = "ctl00$PageContent$DeleteButton$_Button", ""
    base = str(r.url).split("?")[0].rsplit("/", 1)[0] + "/"
    alvo = action if action.startswith("http") else (BASE + action if action.startswith("/") else base + action.lstrip("./"))
    r2 = lg.http.post(alvo, data=campos, headers={"Referer": quote(str(r.url), safe=":/?&=%#;+,@!$'()*~.-_")})
    r2.raise_for_status()
    avisos = re.findall(r"""alert\(\s*['"]([^'"]{3,200})['"]""", r2.text)
    # confere: a ocorrência não pode mais aparecer na busca
    if _linha_da_ocorrencia(_buscar_lista(lg, numero, max(v, 0)).text, numero) or \
            _linha_da_ocorrencia(lg._get(URL_LISTA_OC).text, numero):
        raise RuntimeError("o Leankeep não excluiu" + (f": {' | '.join(avisos)[:200]}" if avisos else ""))
    return "ok"
