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

def processar_leads_sem_resposta(cfg, meta_data, agora_utc, agora_br, clientes_processados):
    print("\n🎯 [MÓDULO 3] Verificando leads sem resposta inicial (Janela 72h/24h)...")
    intervalos_minutos = cfg.get("intervalosMinutos", {})
    instrucoes_etapas = cfg.get("instrucoesEtapas", {})
    # Usa o maior valor entre totalEtapas configurado e a quantidade de etapas cadastradas em intervalosMinutos
    total_etapas = max(int(cfg.get("totalEtapas", 4)), len(intervalos_minutos))
    minimo_mensagens = int(cfg.get("minimoMensagens", 1))
    prompt_template = cfg.get("promptTemplate", "")

    ia_cfg = cfg.get("iaConfig", {})
    nome_ia = ia_cfg.get("identidadeNome", "Stella")
    cargo_ia = ia_cfg.get("cargoIdentidade", "Consultora Digital de Vendas")

    total_reativados = 0

    for loja_id in LOJAS_REDE:
        clientes_ref = db.collection("conversas").document(loja_id).collection("clientes")
        nome_loja = NOMES_LOJAS.get(loja_id, cfg.get("concessionaria", "Grupo Robmar"))

        for doc in clientes_ref.stream():
            cliente_id = doc.id
            chave_unica = f"{loja_id}:{cliente_id}"

            if chave_unica in clientes_processados:
                continue

            dados = doc.to_dict()
            if lead_esta_finalizado(dados):
                continue

            etapa_atual = int(dados.get("reativacaoEtapa", 0))
            if etapa_atual >= total_etapas:
                continue

            msgs_stream = (
                doc.reference.collection("mensagens")
                .order_by("timestamp", direction=firestore.Query.DESCENDING)
                .limit(10)
                .stream()
            )
            historico = [m.to_dict() for m in msgs_stream]

            if len(historico) < minimo_mensagens:
                continue

            # Garante que este módulo atue apenas nos leads que AINDA NÃO responderam
            cliente_respondeu = any(
                (m.get("sender") or "").lower() in ["cliente", "client", "lead", "user", "usuario"]
                for m in historico
            )
            if cliente_respondeu:
                continue

            ultima_msg = historico[0]
            remetente = (ultima_msg.get("sender") or "").lower()
            if remetente not in ["vendedor", "bot", "stella", "ia", "stella ia"]:
                continue

            # Verifica janela de 72h (Campanha FB/IG), 24h (Orgânico) ou Ilimitado (FB/IG)
            janela_ok, horas_restantes, media_source = verificar_janela_aberta(dados, historico, agora_utc)
            if not janela_ok:
                continue

            ts_ultima = parse_timestamp_utc(ultima_msg.get("timestamp"))
            if not ts_ultima:
                continue

            minutos_silencio = (agora_utc - ts_ultima).total_seconds() / 60.0
            proxima_etapa = etapa_atual + 1
            tempo_necessario = intervalos_minutos.get(f"etapa_{proxima_etapa}")

            if tempo_necessario is None or minutos_silencio < float(tempo_necessario):
                continue

            nome = (
                dados.get("cadastralData", {}).get("clientName")
                or dados.get("clientName")
                or "Cliente"
            ).split()[0]

            status_msg = ultima_msg.get("status", "delivered")
            modo = "read" if status_msg == "read" else "delivered"
            status_leitura = (
                "O cliente LEU a mensagem anterior e não respondeu."
                if modo == "read"
                else "O cliente NÃO abriu/visualizou a mensagem anterior."
            )

            historico_cronologico = list(reversed(historico))
            linhas_hist = [
                f"{m.get('senderName') or m.get('sender') or 'Loja'}: {m.get('text')}"
                for m in historico_cronologico if m.get("text")
            ]
            historico_formatado = "\n".join(linhas_hist) if linhas_hist else "(Sem histórico textual recente)"
            objetivo_etapa = instrucoes_etapas.get(f"etapa_{proxima_etapa}", "Reative o contato com cordialidade.")

            if prompt_template:
                prompt = prompt_template.format(
                    identidadeNome=nome_ia,
                    cargoIdentidade=cargo_ia,
                    concessionaria=nome_loja,
                    nomeCliente=nome,
                    statusLeitura=status_leitura,
                    numeroEtapa=proxima_etapa,
                    objetivoEtapa=objetivo_etapa,
                    historicoMensagens=historico_formatado
                )
            else:
                prompt = f"""Você é {nome_ia}, {cargo_ia} da {nome_loja}.
Sua missão é reativar o cliente {nome} de forma cordial e objetiva.
Status: {status_leitura}
Objetivo da Etapa {proxima_etapa}: {objetivo_etapa}
Histórico:
{historico_formatado}
Retorne apenas o texto de 2 a 3 frases para o WhatsApp."""

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
                tipo_acao="reativacao_lead_novo",
                metadados_extra={
                    "reativacaoEtapa": proxima_etapa,
                    "totalReativacoes": proxima_etapa
                }
            )

            if sucesso:
                doc.reference.collection("reativacoes").document(f"etapa_{proxima_etapa}").set({
                    "tipo": "lead_sem_resposta",
                    "etapa": proxima_etapa,
                    "mediaSource": media_source,
                    "horasRestantesJanela": horas_restantes,
                    "statusUltimaMensagemLead": modo,
                    "mensagemEnviada": texto_envio,
                    "enviadoPor": nome_ia,
                    "geradoPorIa": True,
                    "timestamp": agora_iso,
                    "tempoSilencioMinutos": round(minutos_silencio, 2)
                })
                clientes_processados.add(chave_unica)
                total_reativados += 1

    print(f"🎯 [MÓDULO 3] Concluído. Leads novos reativados: {total_reativados}")
