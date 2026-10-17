ARG PYTHON_BASE=python:3.11-slim
FROM ${PYTHON_BASE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home assistant
COPY src/line_assistant/ ./src/line_assistant/
COPY scripts/container_entrypoint.py ./scripts/container_entrypoint.py
COPY scripts/verify_signed_startup.py ./scripts/verify_signed_startup.py
COPY scripts/verify_history_signed_startup.py ./scripts/verify_history_signed_startup.py
USER 10001
EXPOSE 8080
CMD ["python", "scripts/verify_history_signed_startup.py"]
