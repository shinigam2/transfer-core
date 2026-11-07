FROM python:3.12-slim



ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv


COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


COPY app ./app
COPY migrations ./migrations


EXPOSE 8080

# Команда запуска при старте контейнера:
# запустить сервер uvicorn, взять объект `app` из файла `app/main.py` (`app.main:app`),
# слушать все сетевые интерфейсы контейнера (`0.0.0.0`) на порту `8080`.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]