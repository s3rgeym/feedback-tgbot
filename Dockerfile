# Используем минимальный образ Python на базе Alpine
FROM python:3.12-alpine

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем необходимые пакеты для работы с изображениями и шрифтами
RUN apk add --no-cache build-base

# Копируем файлы приложения в контейнер
COPY . .

# Устанавливаем зависимости Python
RUN pip install --no-cache-dir -r requirements.txt

# Команда для запуска приложения
CMD ["python", "bot.py", "-vv"]
