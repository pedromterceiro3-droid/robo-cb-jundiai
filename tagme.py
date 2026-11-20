"""Cliente da Tagme: login, fila de espera e reservas (mesma API do painel apps.tagme.com.br)."""
from __future__ import annotations

import html as _html
import json
import logging
import re
import secrets
import time
from urllib.parse import parse_qs, urljoin, urlparse
from datetime import date

import httpx

log = logging.getLogger("robo")
API = "https://api.apps.tagme.com.br"
SSO_AUTH = "https://sso.tagme.com.br/realms/ditti-painel-realm/protocol/openid-connect/auth"
REDIRECT = "https://apps.tagme.com.br/auth"
NAVEGADOR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


def _procurar(obj, *chaves):
    """Procura a primeira chave (em qualquer nível) com valor texto."""
    if isinstance(obj, dict):
        for k in chaves:
            if isinstance(obj.get(k), str) and obj[k].strip():
                return obj[k]
        for v in obj.values():
            r = _procurar(v, *chaves)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _procurar(v, *chaves)
            if r:
                return r
    return None


def _itens(resp):
    """Aceita lista direta ou {data|items|entries|results: [...]}."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("data", "items", "entries", "results", "reservations", "docs", "rows"):
            v = resp.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                r = _itens(v)
                if r:
                    return r
    return []


def _total(resp):
    if isinstance(resp, dict):
        for k in ("total", "totalItems", "count", "totalCount"):
            if isinstance(resp.get(k), int):
                return resp[k]
        for k in ("meta", "pagination", "data"):
            if isinstance(resp.get(k), dict):
                t = _total(resp[k])
                if t is not None:
                    return t
    return None


class Tagme:
    def __init__(self, email: str, senha: str, venue_id: str, arquivo_sessao=None,
                 venue_slug: str = "coco-bambu-jundiai"):
        self.email, self.senha, self.venue_id, self.venue_slug = email, senha, venue_id, venue_slug
        self.arquivo_sessao = arquivo_sessao
        self.token: str | None = None
        self.refresh: str | None = None
        self.http = httpx.Client(timeout=httpx.Timeout(40, connect=20),
                                 headers={**NAVEGADOR, "Accept": "application/json",
                                          "Origin": "https://apps.tagme.com.br",
                                          "Referer": "https://apps.tagme.com.br/",
                                          "X-Venue-Id": venue_id, "X-Venue-Slug": venue_slug})
        self._campos_logados = set()
        try:
            d = json.loads(open(arquivo_sessao, encoding="utf-8").read())
            if d.get("email") == email:
                self.token, self.refresh = d.get("token"), d.get("refresh")
        except Exception:
            pass

    # ---------- acesso ----------
    def _salvar(self):
        if self.arquivo_sessao:
            try:
                with open(self.arquivo_sessao, "w", encoding="utf-8") as f:
                    json.dump({"email": self.email, "token": self.token, "refresh": self.refresh}, f)
            except Exception:
                pass

    def _guardar(self, corpo):
        tok = _procurar(corpo, "token", "accessToken", "access_token")
        if not tok:
            raise RuntimeError("a Tagme não devolveu o acesso (token).")
        self.token = tok
        self.refresh = _procurar(corpo, "refreshToken", "refresh_token") or self.refresh
        self._salvar()

    def login(self):
        try:
            self._login_direto()
        except Exception as e:
            log.info("Tagme: login direto não funcionou (%s); tentando pelo Portal de acesso", str(e)[:120])
            self._login_sso()

    def _checar_mfa(self, dados):
        if _procurar(dados, "challengeToken") or (isinstance(dados, dict) and (
                dados.get("mfaRequired") or (dados.get("data") or {}).get("mfaRequired"))):
            raise RuntimeError("a conta da Tagme pede verificação em duas etapas (MFA); "
                               "o robô não consegue entrar sozinho com ela.")

    def _login_direto(self):
        log.info("Tagme: fazendo login direto")
        r = self.http.post(API + "/auth/login", json={"email": self.email, "password": self.senha})
        log.info("Tagme: login direto -> %s", r.status_code)
        if r.status_code >= 300:
            raise RuntimeError(f"{r.status_code}")
        dados = r.json()
        self._checar_mfa(dados)
        self._guardar(dados)
        log.info("Tagme: login OK")

    def _login_sso(self):
        """Mesmo caminho do navegador: tela do Portal de acesso -> código -> troca pelo acesso."""
        sso = httpx.Client(timeout=httpx.Timeout(40, connect=20), headers=NAVEGADOR,
                           follow_redirects=False)
        state = secrets.token_hex(16)
        r = sso.get(SSO_AUTH, params={"client_id": "tagme-reservation", "redirect_uri": REDIRECT,
                                      "response_type": "code", "scope": "openid profile email",
                                      "state": state})
        log.info("Tagme: portal de acesso -> %s", r.status_code)
        m = re.search(r'<form[^>]*action="([^"]+)"', r.text)
        if not m:
            raise RuntimeError(f"não achei o formulário do Portal de acesso (HTTP {r.status_code}).")
        acao = urljoin(str(r.url), _html.unescape(m.group(1)))
        campos = dict(re.findall(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', r.text))
        campos.update({"username": self.email, "password": self.senha, "credentialId": ""})
        r = sso.post(acao, data=campos)
        log.info("Tagme: envio do login -> %s", r.status_code)
        destino = r.headers.get("location", "")
        if r.status_code not in (301, 302, 303) or "code=" not in destino:
            erro = re.search(r'(?:kc-feedback-text|alert-error|input-error)[^>]*>\s*([^<]{3,120})', r.text)
            raise RuntimeError("o Portal de acesso não aceitou o login"
                               + (f": {erro.group(1).strip()}" if erro else
                                  " (confira e-mail e senha no .env; se a conta usa código no celular, "
                                  "o robô não consegue entrar)."))
        code = parse_qs(urlparse(destino).query).get("code", [""])[0]
        ultimo = None
        for corpo in ({"authorizationCode": code, "redirectUri": REDIRECT},):
            r = self.http.post(API + "/auth/sso/exchange", json=corpo)
            log.info("Tagme: troca do código -> %s", r.status_code)
            if r.status_code < 300:
                dados = r.json()
                self._checar_mfa(dados)
                self._guardar(dados)
                log.info("Tagme: login OK (Portal de acesso)")
                return
            ultimo = r
            if r.status_code not in (400, 422):
                break
        raise RuntimeError(f"a Tagme recusou a troca do código ({ultimo.status_code}): {ultimo.text[:150]}")

    def renovar(self):
        if self.refresh:
            r = self.http.post(API + "/auth/refresh", json={"refreshToken": self.refresh})
            if r.status_code < 300:
                try:
                    self._guardar(r.json())
                    return
                except Exception:
                    pass
        self.login()

    def _get(self, caminho, params=None):
        if not self.token:
            self.login()
        for tentativa in range(2):
            r = self.http.get(API + caminho, params=params,
                              headers={"Authorization": f"Bearer {self.token}"})
            if r.status_code in (401, 403) and tentativa == 0:
                self.renovar()
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Tagme respondeu {r.status_code} em {caminho}: {r.text[:200]}")
            return r.json()

    def _logar_campos(self, tipo, itens):
        if tipo in self._campos_logados or not itens:
            return
        self._campos_logados.add(tipo)
        a = itens[0]
        log.info("Tagme: campos de %s: %s", tipo,
                 {k: (type(v).__name__ if isinstance(v, (dict, list)) else
                      ("…" if any(p in k.lower() for p in ("name", "nome", "phone", "email", "cpf", "doc")) else v))
                  for k, v in a.items()})

    # ---------- consultas ----------
    def fila(self) -> list[dict]:
        resp = self._get(f"/v2/waitlist/admin/venues/{self.venue_id}/entries")
        itens = _itens(resp)
        self._logar_campos("fila", itens)
        return itens

    def reservas(self, dia: date) -> list[dict]:
        todas, pagina = [], 1
        while True:
            resp = self._get(f"/reservations/list/venue/{self.venue_id}", params={
                "from": f"{dia.isoformat()}T00:00:00.000Z", "to": f"{dia.isoformat()}T23:59:59.999Z",
                "page": pagina, "limit": 100, "order": json.dumps({"time": "asc", "size": "asc"},
                                                                  separators=(",", ":"))})
            itens = _itens(resp)
            todas += itens
            total = _total(resp)
            if not itens or len(itens) < 100 or (total is not None and len(todas) >= total):
                break
            pagina += 1
            time.sleep(0.3)
        self._logar_campos("reserva", todas)
        return todas
