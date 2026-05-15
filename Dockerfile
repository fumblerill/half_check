# Используем официальный образ Python
FROM python:3.11-slim

# Системные зависимости больше не требуются для OpenCV/PyZbar, 
# так как мы используем только JSON. Оставляем базовый набор.
RUN apt-get update && apt-get install -y \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файл зависимостей
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY calculator.py .

# Запуск бота
CMD ["python", "calculator.py"]
