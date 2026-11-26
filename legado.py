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
    def __init__(self, token: str, refresh: str | None = None, empresa_id: str = "3802"):
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
        self.ult_areas = {}
        self.html = ""
        self.action = ""
        self.campos: dict[str, str] = {}

    # ---------- formulário novo ----------
    def abrir(self):
        from zoneinfo import ZoneInfo
        agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
        r = self.http.get(URL_NOVO + f"&currentDate={agora:%Y-%m-%d}&currentTime={agora:%H-%M}")
        final = str(r.url)
        if r.status_code >= 400 or re.search(r"SignIn|/login|Reauth", final, re.I):
            raise RuntimeError(f"o Leankeep antigo não aceitou o acesso do robô (foi para {final[:80]}, {r.status_code})")
        m = re.search(r'(ctl0\d)[$_]PageContent[$_]TabContainer[$_]TabPanelAnomalia[$_]Descricao', r.text)
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
            raise RuntimeError(f"abri a página, mas não achei o formulário de ocorrência. "
                               f"Endereço: {final[:90]} | status {r.status_code} | título: "
                               f"{(titulo.group(1) if titulo else '?')[:60]} | texto: {corpo[:220]}")
        self._atualizar(r.text, final)
        return self

    def c(self, chave: str) -> str:
        """Nome do campo no formulário (com o prefixo ctl00/ctl01 que a página usou)."""
        return CAMPO[chave].replace("ctl01", self.pre, 1)

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
        if "TabPanelAnomalia" not in r.text or "Descricao" not in r.text:
            corpo = " ".join(unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", r.text,
                                             flags=re.S | re.I)).split())
            raise RuntimeError(f"depois de escolher ({alvo.rsplit('$', 2)[-2]}), o Leankeep não devolveu o "
                               f"formulário: {str(r.url)[:70]} | {corpo[:200]}")
        self._atualizar(r.text, str(r.url))
        return r.text

    def _postback_async(self, alvo: str, extra: dict | None = None):
        """Postback "AJAX" (UpdatePanel), igual ao navegador ao fechar a janelinha de seleção."""
        dados = dict(self.campos)
        dados.update(extra or {})
        dados[f"{self.pre}$scriptManager1"] = f"{self.pre}$PageContent$UpdatePanel1|{alvo}"
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
            if "Descricao" in txt:
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

    def preencher(self, descricao: str, prioridade: str, quando: datetime | None = None):
        q = quando or datetime.now()
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


UNIDADE = ("57896", "CB JUNDIAÍ")


def registrar(token: str, refresh: str | None, grupo: tuple[str, str], area: tuple[str, str], prioridade: str,
              descricao: str, foto: bytes | None = None, quando: datetime | None = None,
              empresa_id: str = "3802", unidade: tuple[str, str] = UNIDADE) -> str:
    """Cria a ocorrência no Leankeep (como o formulário do site). Devolve o número."""
    lg = Legado(token, refresh, empresa_id).abrir()
    lg.escolher_unidade(*unidade)
    if not lg.escolher_grupo(*grupo):
        raise RuntimeError(f"o Leankeep não aceitou o grupo {grupo[1]}")
    if not lg.escolher_area(*area):
        raise RuntimeError(f"o Leankeep não aceitou a área {area[1]}")
    lg.preencher(descricao, prioridade, quando)
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
