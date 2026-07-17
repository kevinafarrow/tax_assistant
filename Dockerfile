FROM python:3.12-slim

RUN useradd --create-home --uid 1000 app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tax_assistant/ tax_assistant/

USER app
EXPOSE 1040

CMD ["uvicorn", "tax_assistant.main:app", "--host", "0.0.0.0", "--port", "1040"]
