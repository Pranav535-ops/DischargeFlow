FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000

# Migrations run separately before rollout (`alembic upgrade head`, e.g. as a one-off task),
# never implicitly on every replica start.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
