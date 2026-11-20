#!/usr/bin/env bash
# Creates the OMR service in Azure. Each step can be re-run. Run one step at a time with
#   SUB=<subscription id> deploy/azure.sh <step>      (steps 1-7 create; step 8 updates; 9-11 below)
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
# Each page is a message on QUEUE; the page worker job runs as many copies as the queue needs (up to
# MAX_WORKERS), so the pages of one PDF are read at the same time. Until 2026-11-20 one worker (OLD_JOB, queue
# OLD_QUEUE) read a whole PDF; it is kept for rolling back (step 11).
JOB=omr-page-worker
QUEUE=omr-pages
OLD_JOB=omr-worker
OLD_QUEUE=omr-jobs
TEST_API=omr-api-next
TAG=${TAG:-$(git -C "$(dirname "$0")" rev-parse --short HEAD)}
IMAGE=$ACR.azurecr.io/omr-service:$TAG
# Worker size, pages converted at once, and how often its trigger checks the queue. 4 vCPU / 8 GiB alone was
# tried on 2026-11-09 and pages were no faster: homr's ONNX Runtime started a thread per host core, more than
# the container's CPUs. With the thread count set per process (homr_runner), 2 pages at once on 4 CPUs took
# 55 s for 5 test pages against 143 s for one page at a time on 2 CPUs (laptop under a CPU limit, 2026-11-13).
# Checking every 5 s instead of 30 s shortened the wait before a job starts from about 55 s to about 31 s.
WORKER_CPU=4
WORKER_MEMORY=8Gi
PAGE_WORKERS=2
QUEUE_POLL_S=5
MAX_WORKERS=10

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
    az storage queue create -n $QUEUE --account-name $SA --auth-mode login -o none ;;

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
        --env-vars STORAGE_ACCOUNT=$SA QUEUE_NAME=$QUEUE AZURE_CLIENT_ID="$(identity clientId)" -o none
    # Until the source is public, allow only one address:  MY_IP=<address> deploy/azure.sh 6
    if [ -n "${MY_IP:-}" ]; then
        az containerapp ingress access-restriction set -g $RG -n $API --rule-name only-me \
            --ip-address "$MY_IP/32" --action Allow -o none
    fi
    az containerapp show -g $RG -n $API --query properties.configuration.ingress.fqdn -o tsv ;;

7)  # page worker: a Container Apps job started by queue messages (checked every $QUEUE_POLL_S s), one run per
    # PAGE_WORKERS pages waiting, up to MAX_WORKERS runs; each run converts pages until the queue is empty.
    # Retries come from the queue (MAX_ATTEMPTS), not from the job.
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
        maxExecutions: $MAX_WORKERS
        pollingInterval: $QUEUE_POLL_S
        rules:
          - name: queue
            type: azure-queue
            metadata:
              accountName: $SA
              queueName: $QUEUE
              queueLength: "$PAGE_WORKERS"
            identity: $(identity id)
  template:
    containers:
      - name: worker
        image: $IMAGE
        command: ["python", "-m", "omr_service.worker"]
        resources:
          cpu: $WORKER_CPU
          memory: $WORKER_MEMORY
        env:
          - name: STORAGE_ACCOUNT
            value: $SA
          - name: AZURE_CLIENT_ID
            value: $(identity clientId)
          - name: QUEUE_NAME
            value: $QUEUE
          - name: PAGE_WORKERS
            value: "$PAGE_WORKERS"
EOF
    az containerapp job create -g $RG -n $JOB --yaml "$yaml" -o none
    rm -f "$yaml" ;;

8)  # update after a commit: build the image, point the API and the page worker at it, apply the worker settings
    az acr build -r $ACR -t omr-service:$TAG -t omr-service:latest "$(dirname "$0")/.."
    az containerapp update -g $RG -n $API --image "$IMAGE" --set-env-vars QUEUE_NAME=$QUEUE -o none
    az containerapp job update -g $RG -n $JOB --image "$IMAGE" --cpu $WORKER_CPU --memory $WORKER_MEMORY \
        --polling-interval $QUEUE_POLL_S --max-executions $MAX_WORKERS \
        --set-env-vars QUEUE_NAME=$QUEUE PAGE_WORKERS=$PAGE_WORKERS -o none ;;

9)  # a second API beside the live one, for trying a new image before switching (step 10); scales to zero
    az containerapp create -g $RG -n $TEST_API --environment $ENV --image "$IMAGE" \
        --user-assigned "$(identity id)" --registry-server $ACR.azurecr.io --registry-identity "$(identity id)" \
        --ingress external --target-port 8000 --cpu 0.5 --memory 1Gi --min-replicas 0 --max-replicas 1 \
        --env-vars STORAGE_ACCOUNT=$SA QUEUE_NAME=$QUEUE AZURE_CLIENT_ID="$(identity clientId)" -o none
    az containerapp show -g $RG -n $TEST_API --query properties.configuration.ingress.fqdn -o tsv ;;

10) # switch the live API to this image and the page queue (the website keeps its address)
    az containerapp update -g $RG -n $API --image "$IMAGE" --set-env-vars QUEUE_NAME=$QUEUE -o none ;;

11) # roll back: the live API to the image before pages were queued separately (TAG=dae3d51) and the old queue,
    # which OLD_JOB still reads
    az containerapp update -g $RG -n $API --image "$IMAGE" --set-env-vars QUEUE_NAME=$OLD_QUEUE -o none ;;

*)  echo "unknown step: $1" >&2; exit 1 ;;
esac
