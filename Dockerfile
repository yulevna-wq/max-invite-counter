FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

# Устанавливаем системное хранилище сертификатов и корневой сертификат НУЦ Минцифры.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL \
       https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
       -o /usr/local/share/ca-certificates/russian_trusted_root_ca.crt \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY russian_trusted_root_ca_pem.crt .
COPY russian_trusted_sub_ca_pem.crt . 

RUN mkdir -p /app/data

CMD ["python", "-u", "main.py"]
