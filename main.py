from datetime import datetime, timezone
from config_e_servicos import FUSO_BR, carregar_configuracoes
from modulo_agendamentos import processar_agendamentos
from modulo_conversas_abertas import processar_conversas_abandonadas
from modulo_leads_sem_resposta import processar_leads_sem_resposta

def main():
    cfg, meta_data = carregar_configuracoes()

    if not cfg.get("ativo", True):
        print("⚠️ Motor de reativação pausado no Firestore (ativo = False). Encerrando.")
        return

    agora_br = datetime.now(FUSO_BR)
    agora_utc = datetime.now(timezone.utc)
    hora_atual = agora_br.hour

    print(f"🕒 Execução iniciada em Brasília: {agora_br.strftime('%d/%m/%Y %H:%M:%S')}")
    if hora_atual < 8 or hora_atual >= 21:
        print("⛔ Fora da janela de atendimento permitida (08:00 às 21:00). Encerrando.")
        return

    # Conjunto para evitar que um mesmo cliente receba duas mensagens na mesma rodada
    clientes_processados = set()

    # Prioridade 1: Confirmação e insistência (1h) de visitas agendadas
    processar_agendamentos(cfg, meta_data, agora_utc, agora_br, clientes_processados)

    # Prioridade 2: Conversas em andamento onde o cliente já interagiu e parou
    processar_conversas_abandonadas(cfg, meta_data, agora_utc, agora_br, clientes_processados)

    # Prioridade 3: Leads recém-chegados que nunca responderam (janela 72h / 24h)
    processar_leads_sem_resposta(cfg, meta_data, agora_utc, agora_br, clientes_processados)

    print(f"\n🏁 Ciclo multi-loja concluído! Total de clientes acionados nesta rodada: {len(clientes_processados)}")

if __name__ == "__main__":
    main()
