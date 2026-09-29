// Flex vs Standard vs Batch demo – all infrastructure in one resource group.
// Deploy twice: first with deployApps=false (infra + ACR), then build the image and deploy with deployApps=true.
targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short prefix used in resource names.')
param prefix string = 'flexdemo'

@description('Public IPs (laptop) allowed to reach the Storage account data plane over the internet.')
param allowedIps array = []

@description('Object ID of the operator (laptop user) – gets data-plane RBAC to send tests and read results.')
param operatorPrincipalId string

@description('Deploy the worker container apps (requires the image to exist in ACR).')
param deployApps bool = false

@description('Worker image tag in ACR.')
param imageTag string = 'latest'

@description('Cron schedule (UTC) of the probe job that sends a 1-prompt test run to the topic. Empty = no probe job.')
param probeCron string = '*/15 * * * *'

var suffix = take(uniqueString(resourceGroup().id), 6)
var tags = {
  project: 'openai-flex-processing'
  SecurityControl: 'Ignore'
}

var flexModel = {
  name: 'gpt-5.6-sol'
  version: '2026-07-09'
  deployment: 'gpt-56-sol'
}
var batchModel = {
  name: 'gpt-5.4-mini'
  version: '2026-03-17'
  deployment: 'gpt-54-mini'
  batchDeployment: 'gpt-54-mini-batch'
}

var topicName = 'llm-tests'
var batchStatusQueue = 'batch-status'
var resultsContainer = 'results'

// ---------------------------------------------------------------- Monitoring
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-law-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// ---------------------------------------------------------------- Network
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${prefix}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [ '10.60.0.0/16' ] }
    subnets: [
      {
        name: 'snet-aca'
        properties: {
          addressPrefix: '10.60.0.0/23'
          delegations: [
            {
              name: 'aca'
              properties: { serviceName: 'Microsoft.App/environments' }
            }
          ]
        }
      }
      {
        name: 'snet-pe'
        properties: {
          addressPrefix: '10.60.2.0/24'
        }
      }
    ]
  }
}

var acaSubnetId = '${vnet.id}/subnets/snet-aca'
var peSubnetId = '${vnet.id}/subnets/snet-pe'

// ---------------------------------------------------------------- Identity
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-workers-id'
  location: location
  tags: tags
}

// ---------------------------------------------------------------- Storage (Entra only, private endpoint)
resource storage 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: '${prefix}st${suffix}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      ipRules: [for ip in allowedIps: { value: ip, action: 'Allow' }]
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' = {
  parent: storage
  name: 'default'
}

resource results 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = {
  parent: blobService
  name: resultsContainer
}

// Blob access logs (caller IP, auth type) – proves workers come in through the private endpoint.
resource blobDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: blobService
  name: 'to-law'
  properties: {
    workspaceId: law.id
    logs: [ { categoryGroup: 'allLogs', enabled: true } ]
  }
}

resource blobDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
  location: 'global'
  tags: tags
}

resource blobDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: blobDnsZone
  name: 'vnet-link'
  location: 'global'
  properties: {
    virtualNetwork: { id: vnet.id }
    registrationEnabled: false
  }
}

resource storagePe 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${storage.name}-blob-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'blob'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: [ 'blob' ]
        }
      }
    ]
  }
}

resource storagePeDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: storagePe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob'
        properties: { privateDnsZoneId: blobDnsZone.id }
      }
    ]
  }
}

// ---------------------------------------------------------------- Service Bus (Entra only)
resource sb 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: '${prefix}-sb-${suffix}'
  location: location
  tags: tags
  sku: { name: 'Standard', tier: 'Standard' }
  properties: {
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
  }
}

resource topic 'Microsoft.ServiceBus/namespaces/topics@2024-01-01' = {
  parent: sb
  name: topicName
  properties: {
    defaultMessageTimeToLive: 'P2D'
  }
}

resource subscriptions 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2024-01-01' = [for s in [ 'standard', 'priority', 'flex', 'batch' ]: {
  parent: topic
  name: s
  properties: {
    lockDuration: 'PT5M'
    maxDeliveryCount: 3
    deadLetteringOnMessageExpiration: true
  }
}]

resource statusQueue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = {
  parent: sb
  name: batchStatusQueue
  properties: {
    lockDuration: 'PT2M'
    maxDeliveryCount: 10
    defaultMessageTimeToLive: 'P2D'
  }
}

// ---------------------------------------------------------------- Foundry (AIServices account + project + deployments)
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: '${prefix}-foundry-${suffix}'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: '${prefix}-foundry-${suffix}'
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: { defaultAction: 'Allow' }
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: '${prefix}-project'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Standard vs Priority vs Flex (+ Batch)'
    description: 'Latency, availability and billing comparison of Standard, Priority and Flex processing, with Batch as a reference.'
  }
}

resource depFlexModel 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: flexModel.deployment
  sku: { name: 'GlobalStandard', capacity: 100 }
  properties: {
    model: { format: 'OpenAI', name: flexModel.name, version: flexModel.version }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [ project ]
}

resource depBatchModelStd 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: batchModel.deployment
  sku: { name: 'GlobalStandard', capacity: 100 }
  properties: {
    model: { format: 'OpenAI', name: batchModel.name, version: batchModel.version }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [ depFlexModel ]
}

resource depBatchModelBatch 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: batchModel.batchDeployment
  sku: { name: 'GlobalBatch', capacity: 100 }
  properties: {
    model: { format: 'OpenAI', name: batchModel.name, version: batchModel.version }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [ depBatchModelStd ]
}

// Private endpoint for Foundry so workers reach the model endpoint over the VNet.
var foundryZones = [
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
]

resource foundryDnsZones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for z in foundryZones: {
  name: z
  location: 'global'
  tags: tags
}]

resource foundryDnsLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (z, i) in foundryZones: {
  parent: foundryDnsZones[i]
  name: 'vnet-link'
  location: 'global'
  properties: {
    virtualNetwork: { id: vnet.id }
    registrationEnabled: false
  }
}]

resource foundryPe 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${foundry.name}-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'account'
        properties: {
          privateLinkServiceId: foundry.id
          groupIds: [ 'account' ]
        }
      }
    ]
  }
  dependsOn: [ depBatchModelBatch, storagePe ]
}

resource foundryPeDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: foundryPe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [for (z, i) in foundryZones: {
      name: replace(z, '.', '-')
      properties: { privateDnsZoneId: foundryDnsZones[i].id }
    }]
  }
}

// ---------------------------------------------------------------- Container registry
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${prefix}acr${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

// ---------------------------------------------------------------- RBAC
var roles = {
  blobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  serviceBusDataOwner: '090c5cfd-751d-490a-894a-3ce6f1109419'
  openAiContributor: 'a001fd3d-188f-4b5d-821b-7da978bf7442'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
}

resource raUamiBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, uami.id, roles.blobDataContributor)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataContributor)
  }
}

// Data Owner is required because the KEDA scaler reads entity runtime properties (message counts).
resource raUamiSb 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: sb
  name: guid(sb.id, uami.id, roles.serviceBusDataOwner)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataOwner)
  }
}

// OpenAI Contributor (data plane) is needed for Files + Batches APIs; it also covers inference.
resource raUamiOpenAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, uami.id, roles.openAiContributor)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiContributor)
  }
}

resource raUamiAcr 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, uami.id, roles.acrPull)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

resource raOpBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, operatorPrincipalId, roles.blobDataContributor)
  properties: {
    principalId: operatorPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataContributor)
  }
}

resource raOpSb 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: sb
  name: guid(sb.id, operatorPrincipalId, roles.serviceBusDataOwner)
  properties: {
    principalId: operatorPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataOwner)
  }
}

resource raOpOpenAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, operatorPrincipalId, roles.openAiContributor)
  properties: {
    principalId: operatorPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiContributor)
  }
}

// ---------------------------------------------------------------- Container Apps environment (VNet integrated, keyless logging)
resource acaEnv 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: '${prefix}-aca-env'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: acaSubnetId
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
  }
}

resource acaEnvDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: acaEnv
  name: 'to-law'
  properties: {
    workspaceId: law.id
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
  }
}

var commonEnv = [
  { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
  { name: 'SERVICEBUS_FQDN', value: '${sb.name}.servicebus.windows.net' }
  { name: 'TOPIC_NAME', value: topicName }
  { name: 'BATCH_STATUS_QUEUE', value: batchStatusQueue }
  { name: 'STORAGE_ACCOUNT_URL', value: storage.properties.primaryEndpoints.blob }
  { name: 'RESULTS_CONTAINER', value: resultsContainer }
  { name: 'OPENAI_BASE_URL', value: 'https://${foundry.properties.customSubDomainName}.openai.azure.com/openai/v1/' }
  { name: 'FLEX_PAIR_DEPLOYMENT', value: flexModel.deployment }
  { name: 'BATCH_PAIR_DEPLOYMENT', value: batchModel.deployment }
  { name: 'BATCH_PAIR_BATCH_DEPLOYMENT', value: batchModel.batchDeployment }
  { name: 'PYTHONUNBUFFERED', value: '1' }
]

var workers = [
  {
    name: 'worker-standard'
    mode: 'standard'
    rules: [ { name: 'sb-topic', topic: topicName, subscription: 'standard', queue: '' } ]
  }
  {
    name: 'worker-priority'
    mode: 'priority'
    rules: [ { name: 'sb-topic', topic: topicName, subscription: 'priority', queue: '' } ]
  }
  {
    name: 'worker-flex'
    mode: 'flex'
    rules: [ { name: 'sb-topic', topic: topicName, subscription: 'flex', queue: '' } ]
  }
  {
    name: 'worker-batch'
    mode: 'batch'
    rules: [
      { name: 'sb-topic', topic: topicName, subscription: 'batch', queue: '' }
      { name: 'sb-status', topic: '', subscription: '', queue: batchStatusQueue }
    ]
  }
]

resource apps 'Microsoft.App/containerApps@2025-07-01' = [for w in workers: if (deployApps) {
  name: w.name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    environmentId: acaEnv.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: '${acr.properties.loginServer}/flex-worker:${imageTag}'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat(commonEnv, [
            { name: 'WORKER_MODE', value: w.mode }
            { name: 'SUBSCRIPTION_NAME', value: w.mode }
          ])
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
        cooldownPeriod: 300
        pollingInterval: 15
        rules: [for r in w.rules: {
          name: r.name
          custom: {
            type: 'azure-servicebus'
            identity: uami.id
            metadata: empty(r.queue) ? {
              namespace: sb.name
              topicName: r.topic
              subscriptionName: r.subscription
              messageCount: '1'
            } : {
              namespace: sb.name
              queueName: r.queue
              messageCount: '1'
            }
          }
        }]
      }
    }
  }
  dependsOn: [ raUamiAcr, raUamiSb, raUamiBlob, raUamiOpenAi, foundryPeDns, storagePeDns, blobDnsLink, foundryDnsLinks, acaEnvDiag ]
}]

// Continuous probe: a scheduled Container Apps Job sends one small test run every 15 minutes, day and night,
// so latency can be sampled over several days without the operator's laptop.
resource probeJob 'Microsoft.App/jobs@2025-07-01' = if (deployApps && !empty(probeCron)) {
  name: 'probe-job'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    environmentId: acaEnv.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: probeCron
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: 300
      replicaRetryLimit: 1
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'probe'
          image: '${acr.properties.loginServer}/flex-worker:${imageTag}'
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: concat(commonEnv, [
            { name: 'WORKER_MODE', value: 'probe' }
            { name: 'PROBE_CAMPAIGN', value: 'probe' }
          ])
        }
      ]
    }
  }
  dependsOn: [ raUamiAcr, raUamiSb, acaEnvDiag ]
}

output resourceGroup string = resourceGroup().name
output storageAccountUrl string = storage.properties.primaryEndpoints.blob
output storageAccountName string = storage.name
output serviceBusFqdn string = '${sb.name}.servicebus.windows.net'
output topicName string = topicName
output openAiBaseUrl string = 'https://${foundry.properties.customSubDomainName}.openai.azure.com/openai/v1/'
output foundryName string = foundry.name
output projectName string = project.name
output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output uamiClientId string = uami.properties.clientId
output flexPairDeployment string = flexModel.deployment
output batchPairDeployment string = batchModel.deployment
output batchPairBatchDeployment string = batchModel.batchDeployment
