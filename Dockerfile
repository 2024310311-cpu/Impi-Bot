# Usar la imagen oficial de Playwright que YA trae los navegadores instalados
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# Configurar el directorio de trabajo
WORKDIR /app

# Copiar los archivos de requerimientos e instalar dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar todo el código del backend a la imagen
COPY . .

# Exponer el puerto en el que correrá FastAPI
EXPOSE 8000

# Comando para iniciar el servidor
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
