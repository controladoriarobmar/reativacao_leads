from datetime import datetime
from google.cloud import firestore
from config_e_servicos import (
    db,
    FUSO_BR,
    NOMES_LOJAS,
    parse_timestamp_utc,
    verificar_janela_aberta,
    lead_esta_finalizado,
    chamar_gemini,
    enviar_e_registrar_mensagem
)

def _normalizar_telefone_br(telefone):
    digits = "".join(filter(str.isdigit, str(telefone or "")))
    if not digits:
        return None
    if not digits.startswith("55") and len(digits) in [10, 11]:
        return f"55{digits}"
    return digits

def processar_agendamentos(cfg, meta_data, agora_utc, agora_br, clientes_processados):
    print("\n📅 [MÓDULO 1] Verificando agendamentos ativos...")
    ia_cfg = cfg.get("iaConfig", {})
    nome_ia = ia_cfg.get("identidadeNome", "Stella")
    cargo_ia = ia_cfg.get("cargoIdentidade", "Consultora Digital de Vendas")

    hoje_str = agora_br.strftime("%Y-%m-%d")

    agendamentos_stream = (
        db.collection("agendamentos")
        .where("Status", "==", "Agendada")
        .where("Finalizado", "==", "Não")
        .stream()
    )

    total_disparos = 0

    for ag_doc in agendamentos_stream:
        ag = ag_doc.to_dict()
        data_visita = ag.get("Data")
        hora_visita = ag.get("Hora", "10:00")
        loja_id = ag.get("storeId") or "robmar-carros"

        if not data_visita or data_visita < hoje_str:
            continue

        if str(ag.get("Confirmado", "")).lower() in ["sim", "true", "confirmado"]:
            continue

        telefone = _normalizar_telefone_br(ag.get("Telefone"))
        if not telefone or f"{loja_id}:{telefone}" in clientes_processados:
            continue

        ultima_tentativa_ts = parse_timestamp_utc(ag.get("ultimaConfirmacaoEm"))
        tentativas = int(ag.get("tentativasConfirmacao", 0))
        is_hoje = (data_visita == hoje_str)

        if is_hoje:
            try:
                dt_visita = datetime.strptime(f"{data_visita} {hora_visita}", "%Y-%m-%d %H:%M").replace(tzinfo=FUSO_BR)
                if agora_br >= dt_visita:
                    continue
            except ValueError:
                pass

            # Agendamentos do dia: insiste de 1 em 1 hora (60 minutos)
            if ultima_tentativa_ts:
                minutos_desde_ultimo = (agora_utc - ultima_tentativa_ts).total_seconds() / 60.0
                if minutos_desde_ultimo < 60.0:
                    continue
        else:
            # Agendamentos futuros: acompanha com intervalo mínimo de 24h (1440 minutos)
            if ultima_tentativa_ts:
                minutos_desde_ultimo = (agora_utc - ultima_tentativa_ts).total_seconds() / 60.0
                if minutos_desde_ultimo < 1440.0:
                    continue

        doc_cliente_ref = db.collection("conversas").document(loja_id).collection("clientes").document(telefone)
        doc_cliente = doc_cliente_ref.get()

        if not doc_cliente.exists:
            continue

        dados_cliente = doc_cliente.to_dict()
        if lead_esta_finalizado(dados_cliente):
            continue

        msgs_stream = (
            doc_cliente_ref.collection("mensagens")
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(8)
            .stream()
        )
        historico = [m.to_dict() for m in msgs_stream]

        # Se o cliente respondeu após o último toque, aguarda resposta do fluxo antes de insistir
        if historico and ultima_tentativa_ts:
            ultima_msg = historico[0]
            if (ultima_msg.get("sender") or "").lower() in ["cliente", "client", "lead", "user", "usuario"]:
                ts_msg_cliente = parse_timestamp_utc(ultima_msg.get("timestamp"))
                if ts_msg_cliente and ts_msg_cliente > ultima_tentativa_ts:
                    continue

        if not dados_cliente.get("mediaSource") and ag.get("Mídia"):
            dados_cliente["mediaSource"] = ag.get("Mídia")

        janela_ok, _, _ = verificar_janela_aberta(dados_cliente, historico, agora_utc)
        if not janela_ok:
            continue

        nome_cliente = (ag.get("Nome") or dados_cliente.get("clientName") or "Cliente").split()[0]
        veiculo = ag.get("Veículo") or "veículo de interesse"
        nome_unidade = NOMES_LOJAS.get(loja_id, ag.get("Unidade") or "nossa loja")
        data_br = datetime.strptime(data_visita, "%Y-%m-%d").strftime("%d/%m")

        if is_hoje:
            contexto_momento = (
                f"A visita está agendada para HOJE ({data_br}) às {hora_visita} na {nome_unidade}. "
                f"Esta é a tentativa de confirmação nº {tentativas + 1}. "
                "Peça de forma simpática, leve e objetiva para o cliente confirmar se está tudo certo para o horário combinado."
            )
        else:
            contexto_momento = (
                f"A visita está agendada para o dia {data_br} às {hora_visita} na {nome_unidade}. "
                "Faça um acompanhamento cordial reforçando que o veículo e o consultor estarão aguardando, "
                "perguntando se ele precisa de alguma informação adicional ou localização antes do dia."
            )

        prompt = f"""Você é {nome_ia}, {cargo_ia} da {nome_unidade}.
Cliente: {nome_cliente}
Veículo agendado: {veiculo}
Situação: {contexto_momento}

Escreva uma mensagem curta e natural para WhatsApp (máximo 3 frases), sem parecer robótica e sem usar colchetes."""

        texto_envio = chamar_gemini(prompt, ia_cfg)
        if not texto_envio:
            continue

        sucesso = enviar_e_registrar_mensagem(
            doc_cliente_ref=doc_cliente_ref,
            dados_cliente=dados_cliente,
            texto=texto_envio,
            meta_data=meta_data,
            ia_cfg=ia_cfg,
            agora_utc=agora_utc,
            agora_br=agora_br,
            tipo_acao="confirmacao_agendamento"
        )

        if sucesso:
            agora_iso = agora_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            ag_doc.reference.update({
                "ultimaConfirmacaoEm": agora_iso,
                "tentativasConfirmacao": tentativas + 1
            })
            clientes_processados.add(f"{loja_id}:{telefone}")
            total_disparos += 1

    print(f"📅 [MÓDULO 1] Concluído. Confirmações enviadas: {total_disparos}")
