# Atajos para no recordar cada comando. Uso: make <objetivo>
TF_DIR      := terraform
BOOTSTRAP   := $(TF_DIR)/bootstrap
INFRA       := $(TF_DIR)/infra

.PHONY: help bootstrap init validate plan apply image destroy fmt outputs

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

image:           ## Paso 3: construye y sube la imagen de la API a ECR
	./scripts/push-image.sh

outputs:         ## Muestra URLs y nombres útiles
	cd $(INFRA) && terraform output

destroy:         ## Borra la infra (el bucket del state se conserva)
	cd $(INFRA) && terraform destroy
