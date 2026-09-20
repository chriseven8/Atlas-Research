FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.lock /app/backend/requirements.lock
RUN pip install --no-cache-dir -r backend/requirements.lock
COPY backend /app/backend
RUN pip install --no-cache-dir --no-deps /app/backend
RUN useradd --create-home research && mkdir -p /app/data && chown research:research /app/data
USER research
EXPOSE 8000
CMD ["uvicorn", "financial_research.api:app", "--host", "0.0.0.0", "--port", "8000"]
