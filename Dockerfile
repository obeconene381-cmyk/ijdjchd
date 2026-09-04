
FROM python:3.12-alpine

RUN apk add --no-cache curl bash unzip procps

RUN curl -L https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip -o /tmp/xray.zip && \
    mkdir -p /usr/local/bin /usr/local/etc/xray && \
    unzip /tmp/xray.zip -d /usr/local/bin/ && \
    chmod +x /usr/local/bin/xray && \
    rm /tmp/xray.zip

RUN pip install --no-cache-dir redis

COPY proxy.py /proxy.py
COPY manager.py /manager.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
