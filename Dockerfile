FROM python:3.13.15-slim@sha256:7e3a6aca9d74f93cca21a91d86a8dad8c34749afd5b4a98ee481c9c47b9f5ed4

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt \
    && python -m pip check

COPY app.py .
COPY mediabot ./mediabot

USER 1000:1000

CMD ["sh", "-c", "umask 027 && exec python -u app.py"]
