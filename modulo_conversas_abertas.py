from google.cloud import firestore
from config_e_servicos import (
    db,
    LOJAS_REDE,
    NOMES_LOJAS,
    parse_timestamp_utc,
    verificar_janela_aberta,
    lead_esta_finalizado,
    chamar_gemini,
    enviar_e_registrar_mensagem
)

# Cadência padrão em minutos caso não esteja definida no Firestore (intervalosAbandonoMinutos)
INTERVALOS_PADRAO_ABANDONO = {
    "etapa_1": 180,   # 3 horas após a última mensagem da loja/IA
    "etapa_2": 1440,  # 24 horas após o 1º follow-up (aproveita janela de 72h)
    "etapa_3": 2880   # 48 horas após o 2º follow-up (aproveita janela de 72h)
}

def processar_conversas_abandonadas(cfg, meta_data, agora_utc, agora_br, clientes_processados):
    print("\n💬 [MÓDULO 2] Verificando conversas em andamento abandonadas...")
    ia_cfg = cfg.get("iaConfig", {})
    nome_ia = ia_cfg.get("identidadeNome", "Stella")
    cargo_ia = ia_cfg.get("cargoIdentidade", "Consultora Digital de Vendas")

    # Permite controlar intervalos e total de etapas também pelo Firestore, se desejar
    intervalos_abandono = cfg.get("intervalosAbandonoMinutos", INTERVALOS_PADRAO_ABANDONO)
    max_etapas_abandono = int(cfg.get("totalEtapasAbandono", len(intervalos_abandono)))

    total_resgates = 0

    for loja_id in LOJAS_REDE:
        clientes_ref = db.collection("conversas").document(loja_id).collection("clientes")
        for doc in clientes_ref.stream():
            cliente_id = doc.id
            chave_unica = f"{loja_id}:{cliente_id}"

            if chave_unica in clientes_processados:
                continue

            dados = doc.to_dict()
            if lead_esta_finalizado(dados):
                continue

            etapa_abandono = int(dados.get("abandonoEtapa", 0))
            if etapa_abandono >= max_etapas_abandono:
                continue

            # Carrega as últimas 12 mensagens da conversa
            msgs_stream = (
                doc.reference.collection("mensagens")
                .order_by("timestamp", direction=firestore.Query.DESCENDING)
                .limit(12)
                .stream()
            )
            historico = [m.to_dict() for m in msgs_stream]
            if not historico:
                continue

            # Verifica se o cliente JÁ interagiu pelo menos uma vez na conversa
            cliente_ja_interagiu = any(
                (m.get("sender") or "").lower() in ["cliente", "client", "lead", "user", "usuario"]
                for m in historico
            )
            if not cliente_ja_interagiu:
                continue  # Pertence ao Módulo 3 (Leads sem resposta inicial)

            # A última mensagem precisa ser da loja/vendedor/IA (aguardando resposta do cliente)
            ultima_msg = historico[0]
            remetente_ultima = (ultima_msg.get("sender") or "").lower()
            if remetente_ultima in ["cliente", "client", "lead", "user", "usuario"]:
                continue

            # Valida se a janela (72h Campanha / 24h Orgânico / Ilimitado Direct) ainda está aberta
            janela_ok, horas_restantes, media_source = verificar_janela_aberta(dados, historico, agora_utc)
            if not janela_ok:
                continue

            ts_ultima = parse_timestamp_utc(ultima_msg.get("timestamp"))
            if not ts_ultima:
                continue

            minutos_silencio = (agora_utc - ts_ultima).total_seconds() / 60.0
            proxima_etapa = etapa_abandono + 1
            tempo_exigido = intervalos_abandono.get(f"etapa_{proxima_etapa}")

            if tempo_exigido is None or minutos_silencio < float(tempo_exigido):
                continue

            # Monta histórico cronológico para a IA retomar exatamente do ponto em que pararam
            historico_cronologico = list(reversed(historico))
            linhas_hist = []
            for m in historico_cronologico:
                txt = m.get("text")
                if txt:
                    rem = m.get("senderName") or m.get("sender") or "Atendimento"
                    linhas_hist.append(f"{rem}: {txt}")

            historico_str = "\n".join(linhas_hist[-8:])
            nome_cliente = (
                dados.get("cadastralData", {}).get("clientName")
                or dados.get("clientName")
                or "Cliente"
            ).split()[0]
            nome_loja = NOMES_LOJAS.get(loja_id, "Grupo Robmar")

            prompt = f"""Você é {nome_ia}, {cargo_ia} da {nome_loja}.
O cliente {nome_cliente} já havia iniciado atendimento conosco, mas parou de responder após a última mensagem.
Esta é a tentativa de retomada nº {proxima_etapa} (ainda dentro da janela de atendimento).

Histórico recente da conversa:
{historico_str}

Instrução: Leia o contexto acima e escreva uma mensagem curta (2 a 3 frases) para WhatsApp retomando o assunto exato em que a conversa parou, incentivando o cliente a responder de forma natural e consultiva. Retorne apenas o texto da mensagem."""

            texto_envio = chamar_gemini(prompt, ia_cfg)
            if not texto_envio:
                continue

            agora_iso = agora_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            sucesso = enviar_e_registrar_mensagem(
                doc_cliente_ref=doc.reference,
                dados_cliente=dados,
                texto=texto_envio,
                meta_data=meta_data,
                ia_cfg=ia_cfg,
                agora_utc=agora_utc,
                agora_br=agora_br,
                tipo_acao="resgate_conversa_aberta",
                metadados_extra={"abandonoEtapa": proxima_etapa}
            )

            if sucesso:
                doc.reference.collection("reativacoes").document(f"abandono_etapa_{proxima_etapa}").set({
                    "tipo": "conversa_aberta",
                    "etapa": proxima_etapa,
                    "mediaSource": media_source,
                    "horasRestantesJanela": horas_restantes,
                    "mensagemEnviada": texto_envio,
                    "enviadoPor": nome_ia,
                    "timestamp": agora_iso
                })
                clientes_processados.add(chave_unica)
                total_resgates += 1

    print(f"💬 [MÓDULO 2] Concluído. Conversas retomadas: {total_resgates}")
