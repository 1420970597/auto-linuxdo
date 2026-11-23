FROM python:3.12-slim-bookworm AS chrome

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN test "$(dpkg --print-architecture)" = amd64 \
    && python -c 'from urllib.request import urlretrieve; urlretrieve("https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb", "/tmp/google-chrome.deb")' \
    && apt-get update \
    && apt-get install --yes --no-install-recommends /tmp/google-chrome.deb fonts-noto-cjk \
    && rm -rf /tmp/google-chrome.deb /var/lib/apt/lists/* \
    && google-chrome --version

FROM chrome AS runtime

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 linuxdo \
    && useradd --uid 10001 --gid 10001 --create-home linuxdo \
    && install -d -m 0700 -o linuxdo -g linuxdo /var/lib/linuxdo \
    && install -d /etc/opt/chrome/policies/managed

COPY deploy/chrome-policy.json /etc/opt/chrome/policies/managed/linuxdo.json
COPY linuxdo ./linuxdo
USER 10001:10001
EXPOSE 8765
CMD ["uvicorn", "linuxdo.server:app", "--host", "0.0.0.0", "--port", "8765", "--no-access-log", "--timeout-graceful-shutdown", "10"]

FROM runtime AS test
COPY tests ./tests
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
