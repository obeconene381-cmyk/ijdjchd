FROM python:3.12-slim

# أدوات النظام الضرورية
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    unzip \
    procps \
    && rm -rf /var/lib/apt/lists/*

# تحميل Xray-core
RUN curl -L https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip -o /tmp/xray.zip && \
    mkdir -p /usr/local/bin /usr/local/etc/xray && \
    unzip /tmp/xray.zip -d /usr/local/bin/ && \
    chmod +x /usr/local/bin/xray && \
    rm /tmp/xray.zip

# تثبيت مكتبات Python المطلوبة
RUN pip install --no-cache-dir redis grpcio

# نسخ ملفات المشروع
COPY proxy.py /proxy.py
COPY manager.py /manager.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
