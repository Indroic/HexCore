FROM python:3.12-slim

# Instala dependencias necesarias para compilar el paquete
RUN pip install --no-cache-dir pypiserver[all] build

# Copia el código fuente del módulo al contenedor
WORKDIR /app
COPY . /app

# Crea la carpeta de paquetes y compila el módulo
RUN rm -rf dist/ /packages \
	&& mkdir /packages \
	&& python -m build \
	&& cp dist/* /packages/

EXPOSE 8080

CMD ["python", "-m", "pypiserver", "-p", "8080", "/packages"]
