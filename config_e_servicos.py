import os
import unicodedata
import requests
import dateutil.parser
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import firebase_admin
from google.cloud import firestore

# =====================================================================
# 1. INICIALIZAÇÃO FIREBASE / FIRESTORE
# =====================================================================
DATABASE_ID = os.getenv("DATABASE_ID", "ai-studio-59483fc8-da5a-456d-a48b-6ca587e00652")

if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.Client(database=DATABASE_ID)
FUSO_BR = ZoneInfo("America/Sao_Paulo")

LOJAS_REDE = [
    "robmar-carros",
    "robmar-motos",
    "shineray-belford-roxo",
    "shineray-caxias",
    "shineray-nova-iguacu",
    "shineray-sao-joao",
    "shineray-valqueire"
]

NOMES_LOJAS = {
    "robmar-carros": "Robmar Automóveis (Vila Valqueire)",
    "robmar-motos": "Robmar Motos",
    "shineray-belford-roxo": "Shineray Belford Roxo",
    "shineray-caxias": "Shineray Duque de Caxias",
    "shineray-nova-iguacu": "Shineray Nova Iguaçu",
    "shineray-sao-joao": "Shineray São João de Meriti",
    "shineray-valqueire": "Shineray Vila Valqueire"
}

def carregar_configuracoes():
    cfg_doc = db.collection("configuracoes").document("reativacao_config").get()
    if not cfg_doc.exists:
        cfg_doc = db.collection("reativacao_config").document("padrao").get()
        if not cfg_doc.exists:
            raise Exception("Documento de configuração de reativação não encontrado no Firestore!")

    meta_doc = db.collection("configuracoes").document("crm_meta").get()
    if not meta_doc.exists:
        raise Exception("Documento configuracoes/crm_meta não encontrado!")

    return cfg_doc.to_dict(), meta_doc.to_dict()

# =====================================================================
# 2. VALIDAÇÃO DE JANELA DE CONVERSA (72H / 24H / ILIMITADO)
# =====================================================================
def _normalizar_texto(texto):
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).strip().lower()

def parse_timestamp_utc(raw_ts):
    if not raw_ts:
        return None
    dt = dateutil.parser.isoparse(raw_ts) if isinstance(raw_ts, str) else raw_ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def obter_limite_janela_horas(media_source):
    src = _normalizar_texto(media_source)
    if "campanha facebook" in src or "campanha instagram" in src:
        return 72.0
    if src in ["facebook", "instagram"]:
        return None
    return 24.0

def verificar_janela_aberta(dados_cliente, historico_msgs, agora_utc):
    media_source = dados_cliente.get("mediaSource") or dados_cliente.get("midia") or ""
    limite_horas = obter_limite_janela_horas(media_source)

    if limite_horas is None:
        return True, None, media_source

    ts_referencia = None
    for msg in historico_msgs:
        sender = _normalizar_texto(msg.get("sender"))
        if sender in ["cliente", "client", "lead", "user", "usuario"]:
            ts_referencia = parse_timestamp_utc(msg.get("timestamp"))
            if ts_referencia:
                break

    if not ts_referencia:
        ts_referencia = parse_timestamp_utc(dados_cliente.get("dataEntrada"))

    if not ts_referencia:
        return False, 0.0, media_source

    horas_decorridas = (agora_utc - ts_referencia).total_seconds() / 3600.0
    horas_restantes = (limite_horas - 0.25) - horas_decorridas

    return (horas_restantes > 0), round(horas_restantes, 2), media_source

def lead_esta_finalizado(dados_cliente):
    status_crm = _normalizar_texto(dados_cliente.get("status"))
    if status_crm in ["vendido", "perdido", "desistiu", "cancelado", "finalizado", "encerrado"]:
        return True
    if dados_cliente.get("finalizacaoMotivo") or dados_cliente.get("finalizacaoMotivoId"):
        return True
    if dados_cliente.get("finalizado") is True:
        return True
    return False

# =====================================================================
# 3. GERAÇÃO DE TEXTO VIA GEMINI REST API
# =====================================================================
def chamar_gemini(prompt, ia_cfg):
    api_key = ia_cfg.get("geminiApiKey")
    modelo_principal = ia_cfg.get("modelo", "gemini-3.8-flash")
    temperatura = ia_cfg.get("temperatura", 0.6)
    max_tokens = ia_cfg.get("maxTokens", 300)

    modelos_tentativa = list(dict.fromkeys([modelo_principal, "gemini-flash-lite-latest"]))

    for modelo in modelos_tentativa:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperatura,
                "maxOutputTokens": max_tokens
            }
        }
        try:
            resp = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=15)
            if resp.status_code == 200:
                dados = resp.json()
                cands = dados.get("candidates", [])
                if cands:
                    return cands[0]["content"]["parts"][0]["text"].strip()
            else:
                print(f"⚠️ Erro ao consultar {modelo} ({resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"⚠️ Falha de conexão com {modelo}: {e}")

    return None

# =====================================================================
# 4. DISPARO WHATSAPP CLOUD API E REGISTRO NO HISTÓRICO
# =====================================================================
def enviar_e_registrar_mensagem(doc_cliente_ref, dados_cliente, texto, meta_data, ia_cfg, agora_utc, agora_br, tipo_acao, metadados_extra=None):
    cliente_id = doc_cliente_ref.id
    phone_number_id = dados_cliente.get("destinationPhoneId") or meta_data.get("phoneNumberId")
    token = meta_data.get("token")
    nome_ia = ia_cfg.get("identidadeNome", "Stella IA")

    url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": cliente_id,
        "type": "text",
        "text": {"preview_url": False, "body": texto}
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=12)
    resp_json = resp.json()

    if resp.status_code not in [200, 201]:
        print(f"❌ Erro WhatsApp API ({cliente_id}): {resp_json}")
        return False

    agora_iso = agora_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    hora_formatada = agora_br.strftime("%H:%M")
    wamid = resp_json.get("messages", [{}])[0].get("id") or f"wamid.{tipo_acao}_{int(agora_utc.timestamp())}"

    doc_cliente_ref.collection("mensagens").document(wamid).set({
        "fileName": "",
        "id": wamid,
        "mediaType": None,
        "mediaUrl": None,
        "sender": "stella",
        "senderName": nome_ia,
        "status": "sent",
        "statusUpdatedAt": agora_iso,
        "text": texto,
        "time": hora_formatada,
        "timestamp": agora_iso,
        "wamid": wamid,
        "tipoAutomacao": tipo_acao
    })

    update_data = {
        "ultimaReativacaoEm": agora_iso,
        "ultimoTipoAutomacao": tipo_acao
    }
    if metadados_extra:
        update_data.update(metadados_extra)

    doc_cliente_ref.update(update_data)
    print(f"✅ [{tipo_acao.upper()}] Enviado para {cliente_id} | WAMID: {wamid}")
    return True
