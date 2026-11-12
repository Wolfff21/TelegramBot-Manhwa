# Usamos una imagen oficial de Python 3.12, que es compatible con todas tus librerías
FROM python:3.12-slim

# Evita que Python escriba archivos .pyc y permite ver los logs en tiempo real
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Render asigna un puerto dinámico a través de la variable PORT
ENV PORT=10000

# Directorio de trabajo dentro del contenedor
WORKDIR /app

# Instalamos las dependencias del sistema que necesita Chromium (para Pyppeteer)
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Copiamos e instalamos los requerimientos de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos el resto del código del bot
COPY . .

# Comando para ejecutar el bot
CMD ["python", "bot.py"]
