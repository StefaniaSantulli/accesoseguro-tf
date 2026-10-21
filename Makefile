# Atajos para no recordar cada comando. Uso: make <objetivo>
TF_DIR      := terraform
BOOTSTRAP   := $(TF_DIR)/bootstrap
INFRA       := $(TF_DIR)/infra

.PHONY: help bootstrap init fmt validate layer plan apply deploy grupo outputs destroy test

help:            ## Lista los comandos
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'

bootstrap:       ## Paso 1: crea el bucket del state y genera backend.hcl
	cd $(BOOTSTRAP) && terraform init && terraform apply

init:            ## Paso 2: init de la infra con el backend remoto
	cd $(INFRA) && terraform init -backend-config=backend.hcl

fmt:             ## Formatea todo el código Terraform
	terraform fmt -recursive $(TF_DIR)

validate:        ## Comprueba formato, inicializa y valida los dos proyectos (sin AWS)
	terraform fmt -check -recursive $(TF_DIR)
	cd $(BOOTSTRAP) && terraform init -backend=false -input=false && terraform validate
	cd $(INFRA) && terraform init -backend=false -input=false && terraform validate

plan: layer      ## Muestra qué va a cambiar
	cd $(INFRA) && terraform plan -out=tfplan

apply:           ## Aplica solo infraestructura; para la aplicación completa usar deploy
	cd $(INFRA) && terraform apply tfplan

grupo:           ## Asigna rol: make grupo EMAIL=x@y.com GRUPO=porteros SEDE=1
	python3 scripts/asignar-rol.py --usuario "$(EMAIL)" --grupo "$(GRUPO)" $(if $(SEDE),--sede "$(SEDE)",)

outputs:         ## Muestra URLs y nombres útiles
	cd $(INFRA) && terraform output

destroy: layer   ## Borra la infra (el bucket del state se conserva)
	cd $(INFRA) && terraform destroy

layer:           ## Empaqueta dependencias Linux y código compartido de las Lambdas
	python3 scripts/build-notifications-layer.py

test:            ## Pruebas de Python y frontend (sin AWS)
	python3 -m unittest discover -s tests -v
	node tests/test_frontend_auth.cjs

deploy:          ## Despliega toda la aplicación desde un commit limpio (requiere AWS y Docker)
	python3 scripts/deploy.py --apply --image-tag "$$(git rev-parse HEAD)"
