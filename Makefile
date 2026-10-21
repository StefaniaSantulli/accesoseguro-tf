# Atajos para no recordar cada comando. Uso: make <objetivo>
TF_DIR      := terraform
BOOTSTRAP   := $(TF_DIR)/bootstrap
INFRA       := $(TF_DIR)/infra

.PHONY: help bootstrap init validate plan apply image destroy fmt outputs grupo

help:            ## Lista los comandos
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'

bootstrap:       ## Paso 1: crea el bucket del state y genera backend.hcl
	cd $(BOOTSTRAP) && terraform init && terraform apply

init:            ## Paso 2: init de la infra con el backend remoto
	cd $(INFRA) && terraform init -backend-config=backend.hcl

fmt:             ## Formatea todo el código Terraform
	terraform fmt -recursive $(TF_DIR)

validate: fmt    ## fmt + validate de los dos proyectos
	cd $(BOOTSTRAP) && terraform init -backend=false -input=false && terraform validate
	cd $(INFRA) && terraform validate

plan:            ## Muestra qué va a cambiar
	cd $(INFRA) && terraform plan -out=tfplan

apply:           ## Aplica el último plan
	cd $(INFRA) && terraform apply tfplan

image:           ## Construye y sube la imagen de backend/ a ECR (tag de terraform.tfvars)
	./scripts/push-image.sh

grupo:           ## Asigna rol: make grupo EMAIL=x@y.com GRUPO=porteros SEDE=1
	python3 scripts/asignar-rol.py --usuario "$(EMAIL)" --grupo "$(GRUPO)" $(if $(SEDE),--sede "$(SEDE)",)

outputs:         ## Muestra URLs y nombres útiles
	cd $(INFRA) && terraform output

destroy:         ## Borra la infra (el bucket del state se conserva)
	cd $(INFRA) && terraform destroy

.PHONY: layer test
layer:           ## Empaqueta dependencias Linux y código de notificaciones para Lambda
	python3 scripts/build-notifications-layer.py

test:            ## Pruebas de Python y frontend (sin AWS)
	python3 -m unittest discover -s tests -v
	node tests/test_frontend_auth.cjs
