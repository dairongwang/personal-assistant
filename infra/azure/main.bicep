// Personal Assistant on Azure — one resource group, three managed services.
//
//   Static Web App (PWA: index.html, ngsw.json, hashed bundles — CDN with the caching rules
//                   from apps/web/staticwebapp.config.json)
//   Container App  (Account & Sync Service, FastAPI)            ← linked as the SWA backend (/api/*)
//   PostgreSQL Flexible Server (accounts, billing, encrypted relay)
//
//   az group create -n pa-prod -l westeurope
//   az deployment group create -g pa-prod -f infra/azure/main.bicep \
//        -p pgPassword=$PG_PASSWORD tokenSecret=$TOKEN_SECRET releaseAdminToken=$RELEASE_ADMIN_TOKEN \
//           stripeSecretKey=… stripeWebhookSecret=… containerImage=ghcr.io/you/personal-assistant:1.0.0
//
// Secrets are passed as secure parameters and end up as Container App secrets; for a fuller
// setup put them in Key Vault and reference them from here.

@description('Name prefix for all resources')
param name string = 'pa'
param location string = resourceGroup().location
@description('Static Web App location (limited set of regions)')
param swaLocation string = 'westeurope'
param containerImage string
@secure()
param pgPassword string
@secure()
param tokenSecret string
@secure()
param releaseAdminToken string
// Payment methods (docs/DEPLOYMENT.md "Payments"). A provider is offered once its secret(s) and
// at least one price/plan id are set; leave empty to not offer it. Price/plan ids are not secrets.
@secure()
param stripeSecretKey string = ''
@secure()
param stripeWebhookSecret string = ''
// Prices (decimal strings in billingCurrency) and the flat Lifetime refund (docs/DEPLOYMENT.md "Payments").
param billingCurrency string = 'USD'
param priceProMonthly string = '1.00'
param priceProYearly string = '10.00'
param priceLifetime string = '20.00'
param refundLifetime string = '10.00'
@secure()
param paypalClientSecret string = ''
param paypalClientId string = ''
param paypalWebhookId string = ''
param paypalPlanProMonthly string = ''
param paypalPlanProYearly string = ''
param appEnv string = 'prod'
// Comma-separated e-mails that become administrators when they register/sign in (docs/MEMBERSHIP.md).
param adminEmails string = ''
// Sponsored content for free memberships (docs/ADVERTISING.md): kill switch and the first-party
// hosts creative images may be loaded from (empty = text-only creatives).
param adsEnabled bool = true
param adsImageHosts string = ''
// Ad source (internal | sponsored | affiliate | adsense | microsoft | network) — README "Testing each
// advertisement source". Provider credentials go into the container's environment the same way.
param adsSource string = 'sponsored'

var pgAdmin = 'assistant'
var dbName = 'assistant'

resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${name}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: '${name}-pg-${uniqueString(resourceGroup().id)}'
  location: location
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: pgAdmin
    administratorLoginPassword: pgPassword
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 14, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-12-01-preview' = {
  parent: postgres
  name: dbName
}

// Allow Azure services (the Container App) to reach the server. Tighten with VNet integration later.
resource pgFirewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-12-01-preview' = {
  parent: postgres
  name: 'allow-azure'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${name}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: { customerId: logs.properties.customerId, sharedKey: logs.listKeys().primarySharedKey }
    }
  }
}

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-api'
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: { external: true, targetPort: 8000, transport: 'auto' }
      secrets: [
        { name: 'database-url', value: 'postgresql+psycopg://${pgAdmin}:${pgPassword}@${postgres.properties.fullyQualifiedDomainName}:5432/${dbName}?sslmode=require' }
        { name: 'token-secret', value: tokenSecret }
        { name: 'release-admin-token', value: releaseAdminToken }
        { name: 'stripe-secret-key', value: stripeSecretKey }
        { name: 'stripe-webhook-secret', value: stripeWebhookSecret }
        { name: 'paypal-client-secret', value: paypalClientSecret }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: containerImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'APP_ENV', value: appEnv }
            { name: 'WEB_DIST', value: '' } // the Static Web App serves the PWA
            { name: 'DATABASE_URL', secretRef: 'database-url' }
            { name: 'TOKEN_SECRET', secretRef: 'token-secret' }
            { name: 'RELEASE_ADMIN_TOKEN', secretRef: 'release-admin-token' }
            { name: 'STRIPE_SECRET_KEY', secretRef: 'stripe-secret-key' }
            { name: 'STRIPE_WEBHOOK_SECRET', secretRef: 'stripe-webhook-secret' }
            { name: 'BILLING_CURRENCY', value: billingCurrency }
            { name: 'PRICE_PRO_MONTHLY', value: priceProMonthly }
            { name: 'PRICE_PRO_YEARLY', value: priceProYearly }
            { name: 'PRICE_LIFETIME', value: priceLifetime }
            { name: 'REFUND_LIFETIME', value: refundLifetime }
            { name: 'PAYPAL_ENV', value: appEnv == 'prod' ? 'live' : 'sandbox' }
            { name: 'PAYPAL_CLIENT_ID', value: paypalClientId }
            { name: 'PAYPAL_CLIENT_SECRET', secretRef: 'paypal-client-secret' }
            { name: 'PAYPAL_WEBHOOK_ID', value: paypalWebhookId }
            { name: 'PAYPAL_PLAN_PRO_MONTHLY', value: paypalPlanProMonthly }
            { name: 'PAYPAL_PLAN_PRO_YEARLY', value: paypalPlanProYearly }
            { name: 'ADMIN_EMAILS', value: adminEmails }
            { name: 'ADS_ENABLED', value: adsEnabled ? 'true' : 'false' }
            { name: 'ADS_IMAGE_HOSTS', value: adsImageHosts }
            { name: 'ADS_SOURCE', value: adsSource }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/api/v1/health', port: 8000 }, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/api/v1/health', port: 8000 }, periodSeconds: 10 }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 3, rules: [ { name: 'http', http: { metadata: { concurrentRequests: '100' } } } ] }
    }
  }
}

resource web 'Microsoft.Web/staticSites@2023-12-01' = {
  name: '${name}-web'
  location: swaLocation
  sku: { name: 'Standard', tier: 'Standard' } // Standard is needed for linked backends
  properties: { stagingEnvironmentPolicy: 'Enabled', allowConfigFileUpdates: true }
}

// Route /api/* of the Static Web App to the Container App (same origin for the PWA → no CORS).
resource linkedBackend 'Microsoft.Web/staticSites/linkedBackends@2023-12-01' = {
  parent: web
  name: 'api'
  properties: { backendResourceId: api.id, region: location }
}

output webHostname string = web.properties.defaultHostname
output apiFqdn string = api.properties.configuration.ingress.fqdn
output staticWebAppName string = web.name
