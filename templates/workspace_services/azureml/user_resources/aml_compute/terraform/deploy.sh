#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

export TF_LOG=""

terraform init -input=false -backend=true -reconfigure \
    -backend-config="resource_group_name=${TF_VAR_mgmt_resource_group_name?}" \
    -backend-config="storage_account_name=${TF_VAR_mgmt_storage_account_name?}" \
    -backend-config="container_name=${TF_VAR_terraform_state_container_name?}" \
    -backend-config="key=tre-user-resource-aml-compute-instance-${TF_VAR_id?}"
terraform plan
terraform apply -auto-approve
