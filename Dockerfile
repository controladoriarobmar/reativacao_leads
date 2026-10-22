FROM python:3.11-slim

# Evita a geração de ficheiros .pyc e força a saída de logs diretamente para a consola do Cloud Run
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Instala as dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código da aplicação
COPY main.py .

CMD ["python", "main.py"]
