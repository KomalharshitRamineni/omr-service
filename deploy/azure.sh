#!/usr/bin/env bash
# Creates the OMR service in Azure. Each step can be re-run. Run one step at a time with
#   SUB=<subscription id> deploy/azure.sh <step>      (steps 1-7; see below)
# Needs the Azure CLI signed in (`az login`) as someone who can create resources and assign roles.
set -euo pipefail

SUB=${SUB:?set SUB to the subscription id}
RG=rg-omr-service
LOC=uksouth
ACR=acromrservice
SA=stomrservice
ID=id-omr-service
ENV=cae-omr-service
API=omr-api
JOB=omr-worker
TAG=${TAG:-$(git -C "$(dirname "$0")" rev-parse --short HEAD)}
IMAGE=$ACR.azurecr.io/omr-service:$TAG

az() { command az "$@" --subscription "$SUB"; }
identity() { az identity show -g $RG -n $ID --query "$1" -o tsv; }

case ${1:?give a step number} in
1)  # resource group
    az group create -n $RG -l $LOC --tags component=omr-service -o none ;;

2)  # container registry, and the image built in Azure from this folder
    az provider register -n Microsoft.ContainerRegistry --wait
    az acr create -g $RG -n $ACR --sku Basic -o none
    az acr build -r $ACR -t omr-service:$TAG -t omr-service:latest "$(dirname "$0")/.." ;;

3)  # storage: blob container for jobs, queue of job ids; Entra ID sign-in only (no shared keys).
    # Creating the container and queue needs the Storage Blob/Queue Data Contributor roles for whoever runs this.
    az storage account create -g $RG -n $SA -l $LOC --sku Standard_LRS --kind StorageV2 --access-tier Hot \
        --min-tls-version TLS1_2 --allow-blob-public-access false --allow-shared-key-access false --https-only true -o none
    az storage container create -n jobs --account-name $SA --auth-mode login -o none
    az storage queue create -n omr-jobs --account-name $SA --auth-mode login -o none ;;

4)  # managed identity used by the API and the worker: read and write jobs, pull the image
    az identity create -g $RG -n $ID -l $LOC -o none
    principal=$(identity principalId)
    storage=$(az storage account show -g $RG -n $SA --query id -o tsv)
    for role in "Storage Blob Data Contributor" "Storage Queue Data Contributor"; do
        az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal \
            --role "$role" --scope "$storage" -o none
    done
    az role assignment create --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal \
        --role AcrPull --scope "$(az acr show -n $ACR --query id -o tsv)" -o none ;;

5)  # Container Apps environment, Consumption plan only (no fixed fee)
    az provider register -n Microsoft.App --wait
    az containerapp env create -g $RG -n $ENV -l $LOC -o none ;;

6)  # API: scales to zero when idle, as it keeps nothing in memory
    az containerapp create -g $RG -n $API --environment $ENV --image "$IMAGE" \
        --user-assigned "$(identity id)" --registry-server $ACR.azurecr.io --registry-identity "$(identity id)" \
        --ingress external --target-port 8000 --cpu 0.5 --memory 1Gi --min-replicas 0 --max-replicas 2 \
        --env-vars STORAGE_ACCOUNT=$SA AZURE_CLIENT_ID="$(identity clientId)" -o none
    # Until the source is public, allow only one address:  MY_IP=<address> deploy/azure.sh 6
    if [ -n "${MY_IP:-}" ]; then
        az containerapp ingress access-restriction set -g $RG -n $API --rule-name only-me \
            --ip-address "$MY_IP/32" --action Allow -o none
    fi
    az containerapp show -g $RG -n $API --query properties.configuration.ingress.fqdn -o tsv ;;

7)  # worker: a Container Apps job started by queue messages (checked every 30 s); each run converts
    # jobs until the queue is empty. Retries come from the queue (MAX_ATTEMPTS), not from the job.
    yaml=$(mktemp --suffix .yaml)
    cat > "$yaml" <<EOF
location: $LOC
identity:
  type: UserAssigned
  userAssignedIdentities:
    "$(identity id)": {}
properties:
  environmentId: $(az containerapp env show -g $RG -n $ENV --query id -o tsv)
  configuration:
    triggerType: Event
    replicaTimeout: 14400
    replicaRetryLimit: 0
    registries:
      - server: $ACR.azurecr.io
        identity: $(identity id)
    eventTriggerConfig:
      parallelism: 1
      replicaCompletionCount: 1
      scale:
        minExecutions: 0
        maxExecutions: 2
        pollingInterval: 30
        rules:
          - name: queue
            type: azure-queue
            metadata:
              accountName: $SA
              queueName: omr-jobs
              queueLength: "1"
            identity: $(identity id)
  template:
    containers:
      - name: worker
        image: $IMAGE
        command: ["python", "-m", "omr_service.worker"]
        resources:
          cpu: 2
          memory: 4Gi
        env:
          - name: STORAGE_ACCOUNT
            value: $SA
          - name: AZURE_CLIENT_ID
            value: $(identity clientId)
EOF
    az containerapp job create -g $RG -n $JOB --yaml "$yaml" -o none
    rm -f "$yaml" ;;

*)  echo "unknown step: $1" >&2; exit 1 ;;
esac
