data "azurerm_virtual_network" "core" {
  name                = local.core_vnet
  resource_group_name = local.core_resource_group_name
}

data "azurerm_subnet" "shared" {
  resource_group_name  = local.core_resource_group_name
  virtual_network_name = local.core_vnet
  name                 = "SharedSubnet"
}

data "azurerm_key_vault" "kv" {
  name                = "kv-${var.tre_id}"
  resource_group_name = local.core_resource_group_name
}

data "azurerm_key_vault_certificate" "nexus_cert" {
  name         = var.ssl_cert_name
  key_vault_id = data.azurerm_key_vault.kv.id
}

data "azurerm_key_vault_secret" "nexus_cert_password" {
  name         = "${data.azurerm_key_vault_certificate.nexus_cert.name}-password"
  key_vault_id = data.azurerm_key_vault.kv.id
}

data "azurerm_storage_account" "nexus" {
  name                = local.storage_account_name
  resource_group_name = local.core_resource_group_name
}

data "azurerm_resource_group" "rg" {
  name = local.core_resource_group_name
}

data "azurerm_private_dns_zone" "nexus" {
  name                = "nexus-${var.tre_id}.${data.azurerm_resource_group.rg.location}.cloudapp.azure.com"
  resource_group_name = local.core_resource_group_name
}

