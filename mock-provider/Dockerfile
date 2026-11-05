FROM python:3.12.7-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd --create-home --uid 10001 mock
USER mock
EXPOSE 9000
CMD ["python", "-m", "app.main"]
