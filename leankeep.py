"""Cliente do Leankeep: login e consulta das atividades pendentes (Backlog)."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date

import httpx

log = logging.getLogger("robo")
AUTH_URL = "https://auth.lkp.app.br/v1/auth"
API_URL = "https://lighthousev2.lkp.app.br"
AGENDA_URL = "https://las.lkp.app.br/v1/Agenda/planoAplicacao"   # a mesma fonte da tela Agenda do site

# Códigos de status usados pelo Leankeep
STATUS = {1: "atrasada", 2: "pendente", 3: "realizada", 4: "aguardando aprovação",
          5: "baixada automaticamente", 6: "não realizada"}


@dataclass
class Atividade:
    numero: str
    descricao: str
    unidade: str
    plano: str
    data: str
    status: str
    executores: list[str] = field(default_factory=list)
    realizada: str | None = None   # data/hora da baixa (quando o Leankeep informa)


def _achar_token(obj):
    """Procura o campo 'token' na resposta do login, onde quer que ele esteja."""
    if isinstance(obj, dict):
        for chave in ("token", "accessToken", "access_token"):
            if isinstance(obj.get(chave), str):
                return obj[chave]
        for v in obj.values():
            t = _achar_token(v)
            if t:
                return t
    elif isinstance(obj, list):
        for v in obj:
            t = _achar_token(v)
            if t:
                return t
    return None


def _achar_chave(obj, chave):
    if isinstance(obj, dict):
        if isinstance(obj.get(chave), str):
            return obj[chave]
        for v in obj.values():
            r = _achar_chave(v, chave)
            if r:
                return r
    return None


def _data_baixa(a: dict):
    """Procura a data/hora em que a atividade foi baixada (o nome do campo pode variar)."""
    for k, v in a.items():
        kl = k.lower()
        if not isinstance(v, str) or len(v) < 10 or not v[:4].isdigit():
            continue
        if any(p in kl for p in ("realiz", "baixa", "execu", "conclu", "finaliz")):
            return v
    return None


def _lista(resp_json, chave=None):
    """Aceita respostas no formato {data: ...} ou direto."""
    d = resp_json
    if isinstance(d, dict) and "data" in d and (chave is None or chave not in d):
        d = d["data"]
    if chave and isinstance(d, dict):
        d = d.get(chave, [])
    return d or []


class Leankeep:
    def __init__(self, usuario: str, senha: str, empresa_id: str, assumir_sessao: bool = False,
                 arquivo_sessao=None):
        self.assumir_sessao = assumir_sessao
        self.arquivo_sessao = arquivo_sessao
        self.refresh: str | None = None
        self.usuario = usuario
        self.senha = senha
        self.empresa_id = str(empresa_id)
        self.token: str | None = None
        self.token_em = 0.0
        self.http = httpx.Client(timeout=httpx.Timeout(45, connect=20))
        self._campos_logados = False
        self._carregar_sessao()

    # ---------- login ----------
    def login(self):
        log.info("Leankeep: fazendo login (assumir sessão=%s)", self.assumir_sessao)
        r = self.http.post(
            AUTH_URL,
            data={"login": self.usuario, "password": self.senha, "platform": 1,
                  "authtoken": "true",
                  "expireCurrentSession": "true" if self.assumir_sessao else "false",
                  "stayConnected": "true"},
            headers={"content-type": "application/x-www-form-urlencoded;charset=utf-8"},
        )
        try:
            corpo = r.json()
        except Exception:
            corpo = {}
        if r.status_code == 401:
            dados = corpo.get("data", corpo) if isinstance(corpo, dict) else {}
            if isinstance(dados, dict) and dados.get("hasActiveSession"):
                raise RuntimeError(
                    "o usuário do Leankeep já está logado em outro lugar (navegador). "
                    "Saia do Leankeep no navegador e reinicie o robô, ou use um usuário só para o robô.")
            raise RuntimeError("usuário ou senha do Leankeep incorretos (confira o arquivo .env).")
        log.info("Leankeep: resposta do login %s", r.status_code)
        if r.status_code >= 400:
            raise RuntimeError(f"login no Leankeep respondeu {r.status_code}: {r.text[:300]}")
        self._guardar_token(corpo)
        log.info("Leankeep: login OK")

    def _guardar_token(self, corpo):
        token = _achar_token(corpo)
        if not token:
            raise RuntimeError("Login no Leankeep não devolveu token. Confira usuário e senha.")
        self.token = token
        self.refresh = _achar_chave(corpo, "refreshToken") or self.refresh
        self.token_em = time.time()
        self._salvar_sessao()

    def _carregar_sessao(self):
        """Reaproveita o acesso salvo, para não fazer login de novo a cada reinício."""
        try:
            d = json.loads(open(self.arquivo_sessao, encoding="utf-8").read())
            if d.get("usuario") == self.usuario:
                self.token, self.refresh = d.get("token"), d.get("refresh")
        except Exception:
            pass

    def _salvar_sessao(self):
        if not self.arquivo_sessao:
            return
        try:
            with open(self.arquivo_sessao, "w", encoding="utf-8") as f:
                json.dump({"usuario": self.usuario, "token": self.token,
                           "refresh": self.refresh}, f)
        except Exception:
            pass

    def renovar(self):
        """Renova o acesso sem fazer login de novo (não disputa a sessão)."""
        if self.refresh:
            r = self.http.post(AUTH_URL + "/refresh",
                               data={"token": self.token, "refreshToken": self.refresh},
                               headers={"content-type": "application/x-www-form-urlencoded;charset=utf-8"})
            if r.status_code == 200:
                try:
                    self._guardar_token(r.json())
                    return
                except Exception:
                    pass
        self.login()

    def _post(self, caminho: str, corpo: dict):
        if not self.token:
            self.login()
        for tentativa in range(2):
            r = self.http.post(
                API_URL + caminho, json=corpo,
                headers={"Authorization": f"Bearer {self.token}",
                         "EmpresaId": self.empresa_id,
                         "Content-Type": "application/json"},
            )
            log.info("Leankeep: %s -> %s", caminho, r.status_code)
            if r.status_code == 401 and tentativa == 0:
                self.renovar()
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Leankeep respondeu {r.status_code} em {caminho}: {r.text[:300]}")
            return r.json()

    def _get_url(self, url: str, params: dict):
        if not self.token:
            self.login()
        for tentativa in range(2):
            r = self.http.get(url, params=params,
                              headers={"Authorization": f"Bearer {self.token}", "EmpresaId": self.empresa_id})
            log.info("Leankeep: %s -> %s", url.split(".br")[-1], r.status_code)
            if r.status_code == 401 and tentativa == 0:
                self.renovar()
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Leankeep respondeu {r.status_code} na Agenda: {r.text[:300]}")
            return r.json()

    def agenda(self, dia: date, baixadas: bool, unidade: str | None = None) -> list[Atividade]:
        """O que a tela AGENDA do site mostra no dia (aba Pendentes ou Baixadas), por plano."""
        import unicodedata

        def sem_acento(t):
            return "".join(c for c in unicodedata.normalize("NFD", str(t or "").upper())
                           if unicodedata.category(c) != "Mn")
        resp = self._get_url(AGENDA_URL, {
            "SelectedDate": f"{dia.isoformat()}T03:00:00.000Z",  # meia-noite de Brasília
            "Baixadas": "true" if baixadas else "false", "DiasAdicionais": 0, "EmpresaId": self.empresa_id})
        itens = resp if isinstance(resp, list) else _lista(resp)
        alvo = sem_acento(unidade).strip() if unidade else ""
        saida = []
        for g in itens:
            if not isinstance(g, dict):
                continue
            if alvo and alvo not in sem_acento(g.get("siteNome")):
                continue
            if not baixadas and g.get("realizada"):
                continue
            nome = str(g.get("planoAtividadeNome") or "").strip()
            saida.append(Atividade(
                numero=f"ag{g.get('aplicacaoPlanoAtividade', '')}",
                descricao=nome + (f" ({g['qtdAtividades']} atividades)" if (g.get("qtdAtividades") or 0) > 1 else ""),
                unidade=str(g.get("siteNome", "")),
                plano=nome,
                data=str(g.get("primeiraDataHora", "")),
                status=STATUS.get(g.get("status"), "pendente"),
                executores=[e.get("nome", "") for e in g.get("executores", []) or [] if e.get("nome")],
                realizada=str(g.get("primeiraDataHora", "")) if baixadas else None,
            ))
        log.info("Leankeep (Agenda %s %s): %s plano(s) da unidade", dia, "baixadas" if baixadas else "pendentes",
                 len(saida))
        return saida

    # ---------- consultas ----------
    def pendentes(self, inicio: date, fim: date, unidade: str | None = None) -> list[Atividade]:
        """Atividades ainda não baixadas com data prevista entre inicio e fim."""
        return self._buscar(inicio, fim, unidade, situacao=1)

    def baixadas(self, inicio: date, fim: date, unidade: str | None = None) -> list[Atividade]:
        """Atividades já baixadas (aba Histórico do Backlog) com data prevista no período."""
        return self._buscar(inicio, fim, unidade, situacao=2)

    def _buscar(self, inicio: date, fim: date, unidade: str | None, situacao: int) -> list[Atividade]:
        filtro = {"situacao": situacao, "dataInicio": inicio.isoformat(), "dataFim": fim.isoformat()}

        # 1) grupos (plano de atividade x aplicação)
        grupos, pagina = [], 1  # o Leankeep numera as páginas a partir de 1
        while True:
            resp = self._post("/v3/atividades/backlog",
                              {**filtro, "pageIndex": pagina, "pageSize": 100})
            itens = _lista(resp, "itens")
            grupos.extend(itens)
            total = resp.get("total") if isinstance(resp, dict) else None
            if total is None and isinstance(resp, dict) and isinstance(resp.get("data"), dict):
                total = resp["data"].get("total")
            if not itens or len(grupos) >= (total or 0):
                break
            pagina += 1

        log.info("Leankeep: %s grupos no total", len(grupos))
        if unidade:
            alvo = unidade.strip().upper()
            grupos = [g for g in grupos if alvo in str(g.get("unidadeNome", "")).upper()]
        if not grupos:
            return []

        log.info("Leankeep: %s grupos da unidade %s", len(grupos), unidade)
        info = {g["aplicacaoPlanoAtividadeId"]: g for g in grupos}

        # 2) atividades de cada grupo (em lotes de 100, 200 por vez)
        resultado: list[Atividade] = []
        pendentes_ids = {gid: 0 for gid in info}
        while pendentes_ids:
            proximos = {}
            por_offset: dict[int, list] = {}
            for gid, off in pendentes_ids.items():
                por_offset.setdefault(off, []).append(gid)
            for off, ids in por_offset.items():
                for i in range(0, len(ids), 100):
                    lote = ids[i:i + 100]
                    resp = self._post("/v3/atividades/backlog/atividades", {
                        **filtro, "aplicacoesIds": lote, "limitePorGrupo": 200,
                        "offset": off, "orderBy": {"column": 1}})
                    for g in _lista(resp):
                        gid = g.get("aplicacaoPlanoAtividadeId")
                        grupo = info.get(gid, {})
                        for a in g.get("atividades", []):
                            # nas pendentes, ignora o que o Leankeep já marca como realizada (3),
                            # aguardando aprovação (4), baixada automaticamente (5) ou não realizada (6)
                            if situacao == 1 and a.get("status") in (3, 4, 5, 6):
                                continue
                            if situacao == 2 and not self._campos_logados:
                                self._campos_logados = True
                                log.info("Leankeep: campos de atividade baixada: %s",
                                         {k: (v if not isinstance(v, (list, dict)) else "…")
                                          for k, v in a.items()})
                            resultado.append(Atividade(
                                numero=str(a.get("numero", "")),
                                descricao=str(a.get("descricao", "")).strip(),
                                unidade=str(grupo.get("unidadeNome", "")),
                                plano=str(grupo.get("planoNome") or "-"),
                                data=str(a.get("data", "")),
                                status=STATUS.get(a.get("status"), "pendente"),
                                executores=[e.get("nome", "") for e in a.get("executores", [])
                                            if e.get("nome")],
                                realizada=_data_baixa(a),
                            ))
                        if g.get("temMais"):
                            proximos[gid] = off + 200
            pendentes_ids = proximos
        log.info("Leankeep: %s atividades encontradas (situação %s)", len(resultado), situacao)
        return resultado
