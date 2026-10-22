import os
import json
import requests
import dateutil.parser
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import firebase_admin
from google.cloud import firestore

# =====================================================================
# 1. INICIALIZAÇÃO FIREBASE / FIRESTORE (NATIVO VIA ADC NA GCP)
# =====================================================================
DATABASE_ID = os.getenv("DATABASE_ID", "ai-studio-59483fc8-da5a-456d-a48b-6ca587e00652")

if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.Client(database=DATABASE_ID)

# =====================================================================
# 2. CARREGAMENTO DAS CONFIGURAÇÕES DINÂMICAS DO FIRESTORE
# =====================================================================
cfg_doc = db.collection("configuracoes").document("reativacao_config").get()
if not cfg_doc.exists:
    cfg_doc = db.collection("reativacao_config").document("padrao").get()
    if not cfg_doc.exists:
        raise Exception("Nenhum documento de configuração de reativação encontrado no Firestore!")

cfg = cfg_doc.to_dict()

# Flag global de ativação
if not cfg.get("ativo", True):
    print("⚠️ Motor de reativação pausado no Firestore (ativo = False). Encerrando execução.")
    exit(0)

LOJA = cfg.get("lojaPadrao", "robmar-carros")
TOTAL_ETAPAS = cfg.get("totalEtapas", 4)
JANELA_MAX_HORAS = cfg.get("janelaMaxHoras", 48)
MINIMO_MENSAGENS = cfg.get("minimoMensagens", 2)

# Configurações de IA
ia_cfg = cfg.get("iaConfig", {})
GEMINI_API_KEY = ia_cfg.get("geminiApiKey")
MODELO_GEMINI = ia_cfg.get("modelo", "gemini-3.8-flash")
TEMPERATURA = ia_cfg.get("temperatura", 0.6)
MAX_TOKENS = ia_cfg.get("maxTokens", 300)
NOME_IA = ia_cfg.get("identidadeNome", "Stella IA")
CARGO_IA = ia_cfg.get("cargoIdentidade", "Consultora Digital de Vendas")
CONCESSIONARIA = ia_cfg.get("concessionaria", "Robmar Carros")

# Intervalos em minutos (lidos dinamicamente da base de dados)
INTERVALOS_MINUTOS = cfg.get("intervalosMinutos", {})
INSTRUCOES_ETAPAS = cfg.get("instrucoesEtapas", {})
PROMPT_TEMPLATE = cfg.get("promptTemplate", "")

# Credenciais da Meta Graph API
meta_doc = db.collection("configuracoes").document("crm_meta").get()
if not meta_doc.exists:
    raise Exception("Documento configuracoes/crm_meta não encontrado!")
meta_data = meta_doc.to_dict()
PHONE_NUMBER_ID = meta_data.get("phoneNumberId")
META_TOKEN = meta_data.get("token")

# =====================================================================
# 3. VALIDAÇÃO DE HORÁRIO COMERCIAL (08:00 às 21:00 - Fuso de Brasília)
# =====================================================================
fuso_brasilia = ZoneInfo("America/Sao_Paulo")
agora_br = datetime.now(fuso_brasilia)
hora_atual = agora_br.hour

print(f"🕒 Hora local em Brasília: {agora_br.strftime('%d/%m/%Y %H:%M:%S')}")
if hora_atual < 8 or hora_atual >= 21:
    print("⛔ Fora da janela de atendimento permitida (08:00 às 21:00). Nenhuma mensagem será disparada.")
    exit(0)

# =====================================================================
# 4. GERADOR DE MENSAGEM VIA GEMINI REST API
# =====================================================================
def gerar_mensagem_contextual(nome_cliente, etapa, modo_leitura, historico_msgs):
    linhas_hist = []
    for m in historico_msgs:
        t = m.get("text")
        if t:
            rem = m.get("senderName") or m.get("sender") or "Desconhecido"
            linhas_hist.append(f"{rem}: {t}")
    historico_formatado = "\n".join(linhas_hist) if linhas_hist else "(Sem histórico textual recente)"

    status_leitura = (
        "O cliente LEU a mensagem anterior e não respondeu."
        if modo_leitura == "read"
        else "O cliente NÃO abriu/visualizou a mensagem anterior."
    )
    
    objetivo_etapa = INSTRUCOES_ETAPAS.get(f"etapa_{etapa}", "Reative o contato com cordialidade.")

    if PROMPT_TEMPLATE:
        prompt = PROMPT_TEMPLATE.format(
            identidadeNome=NOME_IA,
            cargoIdentidade=CARGO_IA,
            concessionaria=CONCESSIONARIA,
            nomeCliente=nome_cliente,
            statusLeitura=status_leitura,
            numeroEtapa=etapa,
            objetivoEtapa=objetivo_etapa,
            historicoMensagens=historico_formatado
        )
    else:
        prompt = f"""Você é {NOME_IA}, {CARGO_IA} da {CONCESSIONARIA}.
Sua missão é reativar o cliente {nome_cliente} de forma cordial e objetiva.
Status: {status_leitura}
Objetivo da Etapa {etapa}: {objetivo_etapa}
Histórico:
{historico_formatado}
Retorne apenas o texto de 2 a 3 frases para o WhatsApp."""

    for modelo in [MODELO_GEMINI, "gemini-flash-latest"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": TEMPERATURA, "maxOutputTokens": MAX_TOKENS}
        }
        try:
            resp = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=12)
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
# 5. DISPARO META CLOUD API
# =====================================================================
def enviar_whatsapp(destinatario, texto):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": destinatario,
        "type": "text",
        "text": {"preview_url": False, "body": texto}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=10)
    return r.status_code, r.json()

# =====================================================================
# 6. EXECUÇÃO DO MOTOR DE REATIVAÇÃO
# =====================================================================
agora_utc = datetime.now(timezone.utc)
agora_iso = agora_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
hora_formatada = agora_br.strftime("%H:%M")

clientes_ref = db.collection("conversas").document(LOJA).collection("clientes")
leads = list(clientes_ref.stream())

print(f"🔍 Total de conversas mapeadas: {len(leads)}")

for doc in leads:
    cliente_id = doc.id
    dados = doc.to_dict()

    # Trava 1: Etapas concluídas
    etapa_atual = dados.get("reativacaoEtapa", 0)
    if etapa_atual >= TOTAL_ETAPAS:
        continue

    # Trava 2: Status comercial
    status_crm = dados.get("status", "").lower()
    if status_crm in ["vendido", "perdido", "desistiu", "cancelado"]:
        continue

    # Consulta mensagens recentes
    msgs_stream = (
        doc.reference.collection("mensagens")
        .order_by("timestamp", direction=firestore.Query.DESCENDING)
        .limit(10)
        .stream()
    )
    historico = [m.to_dict() for m in msgs_stream]

    # Trava 3: Quantidade mínima de mensagens
    if len(historico) < MINIMO_MENSAGENS:
        continue

    ultima_msg = historico[0]
    remetente = ultima_msg.get("sender")
    status_msg = ultima_msg.get("status", "delivered")

    # Trava 4: Apenas se a última mensagem foi da loja/bot
    if remetente not in ["vendedor", "bot", "stella", "ia", "Stella IA"]:
        continue

    raw_ts = ultima_msg.get("timestamp")
    if not raw_ts:
        continue

    msg_time = dateutil.parser.isoparse(raw_ts) if isinstance(raw_ts, str) else raw_ts
    if msg_time.tzinfo is None:
        msg_time = msg_time.replace(tzinfo=timezone.utc)

    segundos_silencio = (agora_utc - msg_time).total_seconds()
    horas_silencio = segundos_silencio / 3600
    minutos_silencio = segundos_silencio / 60

    # Trava 5: Janela máxima de inatividade
    if horas_silencio > JANELA_MAX_HORAS:
        continue

    # Verificação de tempo da etapa no Firestore
    proxima_etapa = etapa_atual + 1
    tempo_necessario = INTERVALOS_MINUTOS.get(f"etapa_{proxima_etapa}")

    if tempo_necessario is None or minutos_silencio < float(tempo_necessario):
        continue

    nome = (
        dados.get("cadastralData", {}).get("clientName")
        or dados.get("clientName")
        or "Cliente"
    ).split()[0]

    modo = "read" if status_msg == "read" else "delivered"
    historico_cronologico = list(reversed(historico))

    print(f"\n🚀 Processando Lead: {nome} ({cliente_id}) | Avançando para Etapa {proxima_etapa}")
    texto_envio = gerar_mensagem_contextual(nome, proxima_etapa, modo, historico_cronologico)

    if not texto_envio:
        print("❌ Mensagem não gerada pela IA. Pulando disparo.")
        continue

    # Disparo no WhatsApp
    status_code, resp_meta = enviar_whatsapp(cliente_id, texto_envio)

    if status_code in [200, 201]:
        wamid = resp_meta.get("messages", [{}])[0].get("id") or f"wamid.reativacao_{proxima_etapa}_{int(agora_utc.timestamp())}"
        print(f"✅ Disparo concluído com sucesso. WAMID: {wamid}")

        # Grava na subcoleção 'reativacoes'
        doc.reference.collection("reativacoes").document(f"etapa_{proxima_etapa}").set({
            "etapa": proxima_etapa,
            "statusUltimaMensagem
