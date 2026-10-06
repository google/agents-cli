# Customer-managed encryption keys (CMEK)

An Agent Runtime agent can be encrypted with your Cloud KMS key instead of a Google-managed one.
The key is set at creation and can't be added, changed, or removed later.

## Where to set the key

| Agent created by | Set the key in |
|---|---|
| `agents-cli deploy` (dev deployments) | `deploy --key` |
| `infra single-project` | `deployment/terraform/single-project/service.tf` |
| `infra cicd` (staging/prod) | `deployment/terraform/cicd/service.tf` |

`--key` only works when `deploy` creates the agent. Terraform-created agents are only updated by
`deploy`, so add the key to the Terraform yourself ([below](#terraform)).

## Key and grants

Use a single-region key in the agent's region, by full name:
`projects/KEY_PROJECT/locations/REGION/keyRings/RING/cryptoKeys/KEY`. The key may live in another project.

Before creation, grant `roles/cloudkms.cryptoKeyEncrypterDecrypter` **on the key** to both
service agents of the project running the agent:

- `service-PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com`
- `service-PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com`

```bash
for sa in gcp-sa-aiplatform gcp-sa-aiplatform-re; do
  gcloud kms keys add-iam-policy-binding KEY \
    --keyring RING --location REGION --project KEY_PROJECT \
    --member "serviceAccount:service-PROJECT_NUMBER@${sa}.iam.gserviceaccount.com" \
    --role roles/cloudkms.cryptoKeyEncrypterDecrypter
done
```

Granting needs `roles/cloudkms.admin` (Editor isn't enough). Grants take up to 10 minutes to apply.

## Dev deployments

```bash
agents-cli deploy --key projects/KEY_PROJECT/locations/REGION/keyRings/RING/cryptoKeys/KEY
```

On redeploy, omit `--key` or pass the same key; anything else fails. To change encryption,
delete and re-create the agent. `deploy --list` shows each agent's key.

## Terraform

Add `encryption_spec` to `google_vertex_ai_reasoning_engine.app` and make it depend on the key
grants. If the grants are managed outside Terraform, skip the IAM resource.

**single-project** (`service.tf`):

```hcl
locals {
  kms_key = "projects/KEY_PROJECT/locations/REGION/keyRings/RING/cryptoKeys/KEY"
}

resource "google_kms_crypto_key_iam_member" "agent_platform_cmek" {
  for_each      = toset(["gcp-sa-aiplatform", "gcp-sa-aiplatform-re"])
  crypto_key_id = local.kms_key
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.project.number}@${each.key}.iam.gserviceaccount.com"
}

resource "google_vertex_ai_reasoning_engine" "app" {
  # ...existing arguments...
  encryption_spec {
    kms_key_name = local.kms_key
  }
  depends_on = [
    google_project_service.services,
    google_kms_crypto_key_iam_member.agent_platform_cmek,
  ]
}
```

**cicd** (`service.tf`), one key per environment, granted to that environment's project:

```hcl
locals {
  kms_keys = {
    staging = "projects/KEY_PROJECT/locations/REGION/keyRings/RING/cryptoKeys/STAGING_KEY"
    prod    = "projects/KEY_PROJECT/locations/REGION/keyRings/RING/cryptoKeys/PROD_KEY"
  }
}

resource "google_kms_crypto_key_iam_member" "agent_platform_cmek" {
  for_each = {
    for pair in setproduct(keys(local.deploy_project_ids), ["gcp-sa-aiplatform", "gcp-sa-aiplatform-re"]) :
    "${pair[0]}-${pair[1]}" => { env = pair[0], agent = pair[1] }
  }
  crypto_key_id = local.kms_keys[each.value.env]
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.projects[each.value.env].number}@${each.value.agent}.iam.gserviceaccount.com"
}

resource "google_vertex_ai_reasoning_engine" "app" {
  # ...existing arguments...
  encryption_spec {
    kms_key_name = local.kms_keys[each.key]
  }
  depends_on = [
    google_project_service.deploy_project_services,
    google_kms_crypto_key_iam_member.agent_platform_cmek,
  ]
}
```

Adding a key to an already deployed agent re-creates it (new resource ID, placeholder source
until the next deploy). Check `terraform plan` first.
