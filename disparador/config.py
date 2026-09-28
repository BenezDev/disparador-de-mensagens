#!/usr/bin/env python3
"""
Configuracao: config.json na raiz + variaveis de ambiente para os segredos.

Token de provedor NAO vai no config.json versionado. A ordem de resolucao e
`env` primeiro, config depois -- assim da para deixar o arquivo de exemplo no
disco sem risco de vazar credencial.
"""
import json, os

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMINHO = os.path.join(RAIZ, "config.json")

PADRAO = {
    # Janela operacional conservadora. Nao e uma citacao do Decreto 11.034:
    # ele regula SAC e exclui oferta/contratacao do seu escopo.
    "janela": {
        "inicio": "09:00", "fim": "19:00",
        "sabado_inicio": "09:00", "sabado_fim": "13:00",
        "domingo": False,
    },
    # Nenhum numero recebe outra mensagem nossa antes disso, em qualquer campanha.
    "cooldown_dias": 45,
    # Disparos ja feitos por FORA deste app (outro disparador). Somado ao total
    # real so no contador do painel, para o numero refletir o acumulado da
    # operacao. Nao cria envio nem contato; nao afeta cota, fila nem rampa.
    "painel": {"envios_offset": 12000},
    "importacao": {
        "diretorios": ["importacoes"],
        "max_bytes": 536870912,
    },
    "teste": {"limite_dia": 5},
    "optout": {
        # 'comandos' sao inequivocos: podem abrir uma resposta curta
        # ("sair por favor", "pare com isso"). Alem do portugues, as palavras
        # do padrao CTIA/FCC que a Twilio reconhece por default -- a Twilio so
        # filtra em ingles, entao o "SAIR" em portugues chega ate nos e tem
        # que ser tratado aqui.
        "comandos": ["sair", "saír", "pare", "parar", "stop", "cancelar", "cancele",
                     "remover", "remova", "descadastrar", "descadastro", "desinscrever",
                     "stopall", "unsubscribe", "cancel", "end", "quit", "revoke",
                     "optout", "opt-out"],
        # 'isoladas' sao palavras comuns demais do portugues: so valem quando
        # a resposta e EXATAMENTE isso. "para qual email?" nao e um opt-out.
        "isoladas": ["nao", "para", "pare", "chega", "nunca", "negativo"],
        # 'frases' sao inequivocas e casam em qualquer lugar do texto.
        "frases": ["nao quero receber", "nao quero mais", "nao envie", "nao envia",
                   "nao me mande", "nao me manda", "nao perturbe", "me tire da lista",
                   "tirar da lista", "remover da lista", "sair da lista",
                   "descadastrar", "parar de receber", "nao tenho interesse"],
        "rodape_sms": "Responda SAIR p/ nao receber.",
        "rodape_whatsapp": "Se nao quiser mais receber, responda SAIR.",
    },
    "provedores": {
        "simulado": {"falha_pct": 3},
        "whatsapp_cloud": {
            "phone_id": "", "token": "", "app_secret": "", "verify_token": "",
            "versao": "v21.0",
        },
        "evolution": {
            "url": "", "instancia": "", "apikey": "", "webhook_secret": "",
            "versao": "2",
        },
        "sms_http": {
            "url": "", "metodo": "POST", "tipo": "json",
            "cabecalhos": {},
            "corpo": {"number": "{destino}", "msg": "{texto}"},
            "campo_id": "id",
            "optout_externo": False,
        },
        "twilio": {"sid": "", "token": "", "de": ""},
    },
    # env que sobrescreve, se existir
    "_env": {
        "whatsapp_cloud.token": "WHATSAPP_TOKEN",
        "whatsapp_cloud.phone_id": "WHATSAPP_PHONE_ID",
        "whatsapp_cloud.app_secret": "WHATSAPP_APP_SECRET",
        "whatsapp_cloud.verify_token": "WHATSAPP_VERIFY_TOKEN",
        "evolution.apikey": "EVOLUTION_APIKEY",
        "evolution.webhook_secret": "EVOLUTION_WEBHOOK_SECRET",
        "twilio.sid": "TWILIO_SID",
        "twilio.token": "TWILIO_TOKEN",
        "sms_http.url": "SMS_URL",
    },
}


def _funde(base, novo):
    for k, v in (novo or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _funde(base[k], v)
        else:
            base[k] = v
    return base


_cache = None


def carregar(recarregar=False):
    global _cache
    if _cache is not None and not recarregar:
        return _cache
    cfg = json.loads(json.dumps(PADRAO))
    if os.path.exists(CAMINHO):
        with open(CAMINHO, encoding="utf-8") as f:
            _funde(cfg, json.load(f))
    for caminho, env in cfg.get("_env", {}).items():
        val = os.environ.get(env)
        if val:
            prov, campo = caminho.split(".")
            cfg["provedores"].setdefault(prov, {})[campo] = val
    _cache = cfg
    return cfg


def _limpar_marcadores(obj):
    if isinstance(obj, dict):
        return {k: _limpar_marcadores(v) for k, v in obj.items()
                if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_limpar_marcadores(v) for v in obj]
    return obj


def _fundir_segredos(novo, atual, e_segredo):
    if not isinstance(novo, dict):
        return novo
    for campo in list(novo):
        valor = novo[campo]
        anterior = atual.get(campo) if isinstance(atual, dict) else None
        if isinstance(valor, dict):
            _fundir_segredos(valor, anterior or {}, e_segredo)
        elif e_segredo(campo) and not str(valor or "").strip():
            novo[campo] = anterior
    return novo


def fundir_segredos(novo):
    """Segredo que voltou vazio do painel mantem o valor ja gravado.

    A tela nunca recebe o token (config_seguro o troca por None), entao salvar
    o formulario de volta apagaria a credencial a cada visita a aba config.
    """
    from .provedores import e_segredo
    from .seguranca import mesma_origem
    atual = carregar()
    novo = _limpar_marcadores(novo)
    for prov, campos in (novo.get("provedores") or {}).items():
        anteriores = atual.get("provedores", {}).get(prov, {})
        origem_mudou = ("url" in campos and
                        not mesma_origem(campos.get("url"), anteriores.get("url")))
        _fundir_segredos(campos, {} if origem_mudou else anteriores, e_segredo)
    return novo


def salvar(cfg):
    global _cache
    limpo = {k: v for k, v in cfg.items() if k != "_env"}
    with open(CAMINHO, "w", encoding="utf-8") as f:
        json.dump(limpo, f, ensure_ascii=False, indent=2)
    os.chmod(CAMINHO, 0o600)          # tem credencial dentro
    _cache = None
    return carregar()
