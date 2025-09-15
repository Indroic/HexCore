# Makefile para generar archivos stub (.pyi) de todo el proyecto

STUBGEN=stubgen
MODULE=hexcore

.PHONY: stubs clean-stubs

stubs:
	PYTHONPATH=. $(STUBGEN) -p $(MODULE) -o .

clean-stubs:
	find . -name '*.pyi' -not -path './venv/*' -delete
