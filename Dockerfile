FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Dependências corretas (corrigido: atlas -> openblas)
RUN apt-get update && apt-get install -y \
    gcc \
    gfortran \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

# Copia requirements primeiro (melhor cache)
COPY requirements.txt .

# Atualiza pip
RUN pip install --upgrade pip

# Instala PyTorch CPU (ANTES do requirements)
RUN pip install --no-cache-dir \
    torch==2.0.1+cpu \
    torchvision==0.15.2+cpu \
    torchaudio==2.0.2+cpu \
    --index-url https://download.pytorch.org/whl/cpu

# Instala restante das libs
RUN pip install --no-cache-dir -r requirements.txt

# Copia código
COPY . .

# Cria diretórios
RUN mkdir -p static templates

EXPOSE 5000

CMD ["python", "main.py"]
