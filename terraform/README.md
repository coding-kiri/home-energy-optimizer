# Terraform: dev and prod

Infrastructure is parameterized by `env` (`dev` or `prod`). **Each environment must have its own Terraform state** (separate S3 `key` in the backend config) and its own var file. Never change `env` in one file and re-apply against the other environment’s state.

## One-time manual setup (AWS / Databricks)

1. **S3 bucket + DynamoDB table** for Terraform state (same account/region as your stacks is typical). The bucket must exist before the first `terraform init` with the S3 backend.
2. **Databricks workspaces** for dev and prod (or your chosen split), each attached to Unity Catalog as required by your account.
3. **`~/.databrickscfg` profiles** matching `databricks_profile` in each tfvars file (defaults: `home-energy-optimizer-dev`, `home-energy-optimizer-prod`).
4. **Unity Catalog catalogs** referenced in [../databricks.yml](../databricks.yml) per target (`catalog` under each `targets.*.variables`). Create empty catalogs and grant your principals as needed. Terraform does not create catalogs today.

## Per-environment files (gitignored)

| Committed example | You copy to (gitignored) |
|--------------------|---------------------------|
| [backend-dev.hcl.example](backend-dev.hcl.example) | `backend-dev.hcl` |
| [backend-prod.hcl.example](backend-prod.hcl.example) | `backend-prod.hcl` |
| [terraform.tfvars.dev.example](terraform.tfvars.dev.example) | `terraform.tfvars.dev` |
| [terraform.tfvars.prod.example](terraform.tfvars.prod.example) | `terraform.tfvars.prod` |

The root module includes a placeholder **S3** `backend` block in [main.tf](main.tf) so `terraform validate` works before you run init. Your real bucket, state `key`, and optional DynamoDB table are supplied via `backend-dev.hcl` / `backend-prod.hcl` (see examples). `terraform init -backend-config=...` **merges** those values and overrides the placeholders.

## First-time bootstrap (repeat for dev, then prod)

From this directory:

```bash
# Dev
terraform init -upgrade -backend-config=backend-dev.hcl
bash bootstrap.sh terraform.tfvars.dev backend-dev.hcl

# Prod (separate state key in backend-prod.hcl)
terraform init -upgrade -reconfigure -backend-config=backend-prod.hcl
bash bootstrap.sh terraform.tfvars.prod backend-prod.hcl
```

Use `-reconfigure` when switching the backend config in the same working copy so Terraform does not mix state backends.

If you are **migrating existing local state** to S3, run the first `terraform init` for that environment with **`-migrate-state`**:

```bash
terraform init -upgrade -migrate-state -backend-config=backend-dev.hcl
```

## Day-two applies

```bash
terraform init -backend-config=backend-dev.hcl   # if .terraform was removed
terraform apply -var-file=terraform.tfvars.dev
```

Use the matching `backend-*.hcl` and `terraform.tfvars.*` for prod.

## Legacy single `terraform.tfvars`

Pass the backend config explicitly (there is no supported path that omits the S3 backend anymore):

```bash
terraform init -upgrade -backend-config=backend-dev.hcl
bash bootstrap.sh terraform.tfvars backend-dev.hcl
```

For **local validation** without touching remote state:

```bash
terraform init -backend=false
terraform validate
```
