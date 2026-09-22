FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
ARG PIP_INDEX_URL=""
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      python3 -m pip install --break-system-packages --no-cache-dir \
        -i "$PIP_INDEX_URL" -r /tmp/requirements.txt; \
    else \
      python3 -m pip install --break-system-packages --no-cache-dir \
        -r /tmp/requirements.txt; \
    fi

COPY . /app
RUN chmod 0755 /app/docker/integration/run-tests.sh

ENTRYPOINT ["/app/docker/integration/run-tests.sh"]
