# SharePoint KPI Sync Framework

A cloud-agnostic Python framework that pulls data from IT Admin APIs and syncs it to SharePoint Online lists, for use as KPI dashboards.

Runs on **AWS Lambda** and **Azure Functions** with the same business logic. Secrets are loaded from AWS SSM/Secrets Manager or Azure Key Vault automatically.

---

## Architecture

```
EventBridge / Timer Trigger
         │
         ▼
  Lambda / Azure Fn
         │
   ┌─────┴──────┐
   │ SyncRunner │
   └─────┬──────┘
         │
   ┌─────▼──────────────────────────────────┐
   │  Connector (EntraID / Meraki / etc.)   │
   │  authenticate() → fetch() → batches   │
   └─────────────────────────────────────────┘
         │
   ┌─────▼──────────────────────────────────┐
   │  SharePointClient                      │
   │  provision_schema() → batch_upsert()  │
   └─────────────────────────────────────────┘
         │
   SharePoint Online List  →  Power BI / KPI dashboard
```

---

## Supported Connectors

| Key         | Source           | Resources                                  |
|-------------|------------------|--------------------------------------------|
| `entraid`   | Microsoft Entra  | `users`, `devices`, `groups`, `signInLogs` |
| `meraki`    | Cisco Meraki     | `devices`, `clients`, `networks`, `inventory` |
| `sendgrid`  | SendGrid         | `stats`, `category_stats`, `subuser_stats` |
| `ninjaone`  | NinjaOne RMM     | `devices`, `alerts`, `organizations`       |
| `generic`   | Any REST API     | Configurable field mapping + pagination    |

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set environment variables (local dev)

```bash
export SP_TENANT_ID=...
export SP_CLIENT_ID=...
export SP_CLIENT_SECRET=...
export SP_SITE_URL=https://contoso.sharepoint.com/sites/KPIs
export SP_TENANT_NAME=contoso
```

### 3. Run locally

```bash
python handlers/lambda_handler.py --event tests/sample_event.json
```

---

## Deploying to AWS Lambda

```bash
cd infrastructure
terraform init
terraform apply \
  -var="sp_tenant_id=YOUR_TENANT_ID" \
  -var="sp_client_id=YOUR_CLIENT_ID" \
  -var="sp_client_secret=YOUR_SECRET" \
  -var="sp_site_url=https://contoso.sharepoint.com/sites/KPIs" \
  -var="sp_tenant_name=contoso" \
  -var='sync_jobs_json=[{"connector":"entraid","connector_config":{"tenant_id":{"$secret":"ENTRAID_TENANT_ID"},...}}]'
```

The Terraform config creates:
- Lambda function (Python 3.12, 15 min timeout)
- IAM role with SSM + Secrets Manager read permissions
- EventBridge rule (hourly by default, override with `schedule_cron`)
- SSM parameters for SharePoint credentials

---

## Deploying to Azure Functions

```bash
# Install Azure Functions Core Tools
npm install -g azure-functions-core-tools@4

# Deploy
cd handlers
func azure functionapp publish YOUR_FUNCTION_APP_NAME
```

Set these Application Settings in Azure Portal:
```
SP_TENANT_ID        = ...
SP_CLIENT_ID        = ...
SP_CLIENT_SECRET    = ...
SP_SITE_URL         = https://contoso.sharepoint.com/sites/KPIs
SP_TENANT_NAME      = contoso
SYNC_CRON           = 0 0 * * * *   (hourly)
SYNC_JOBS_JSON      = [{"connector":"entraid","connector_config":{...}}]
AZURE_KEY_VAULT_URL = https://your-vault.vault.azure.net   (optional)
```

---

## Event / Job Payload

Both Lambda and Azure Functions accept the same JSON job structure:

```json
{
  "jobs": [
    {
      "connector": "entraid",
      "connector_config": {
        "tenant_id": {"$secret": "ENTRAID_TENANT_ID"},
        "client_id": {"$secret": "ENTRAID_CLIENT_ID"},
        "client_secret": {"$secret": "ENTRAID_CLIENT_SECRET"},
        "resource": "users",
        "list_name": "KPI_EntraID_Users"
      }
    }
  ]
}
```

`{"$secret": "KEY_NAME"}` values are resolved at runtime from the secret store. Plain string values are used as-is.

---

## Adding a New Connector

1. Create `connectors/myservice.py`
2. Subclass `BaseConnector`
3. Implement `schema()` and `fetch()`
4. Register in `CONNECTOR_REGISTRY` in both handler files

```python
from core.base_connector import BaseConnector, ConnectorResult, ConnectorSchema

class MyServiceConnector(BaseConnector):

    def schema(self) -> ConnectorSchema:
        return ConnectorSchema(
            list_name=self.config.get("list_name", "KPI_MyService"),
            fields={
                "UniqueKey": "Text",
                "Name":      "Text",
                "Value":     "Number",
                "Timestamp": "DateTime",
            }
        )

    def fetch(self):
        # Call your API here
        for item in my_api.get_items():
            yield ConnectorResult(
                unique_key=item["id"],
                fields={
                    "Name":      item["name"],
                    "Value":     item["value"],
                    "Timestamp": item["created_at"],
                }
            )
```

---

## SharePoint App Registration Setup

1. Go to **Azure Portal → Entra ID → App registrations → New registration**
2. Add API permissions: `Sites.ReadWrite.All` (SharePoint, Application type)
3. Grant admin consent
4. Create a client secret
5. Use the tenant ID, client ID, and secret as `SP_*` environment variables

---

## Project Structure

```
sharepoint-kpi-framework/
├── core/
│   ├── base_connector.py   # Abstract connector + ConnectorResult
│   ├── config.py           # Cloud-agnostic config / secrets loader
│   └── runner.py           # SyncRunner orchestrator
├── connectors/
│   ├── entraid.py          # Entra ID / Microsoft Graph
│   ├── meraki.py           # Cisco Meraki Dashboard API
│   ├── sendgrid_ninjaone.py # SendGrid + NinjaOne
│   └── generic.py          # Generic REST API connector
├── sharepoint/
│   └── client.py           # SharePoint REST client (auth, schema, upsert)
├── handlers/
│   ├── lambda_handler.py   # AWS Lambda entrypoint
│   └── azure_function.py   # Azure Functions entrypoint (v2 model)
├── infrastructure/
│   └── main.tf             # Terraform for AWS Lambda
├── tests/
│   └── sample_event.json   # Example job payloads
└── requirements.txt
```
