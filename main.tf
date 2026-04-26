# ============================================================
# Terraform – AWS Lambda deployment for SharePoint KPI Sync
# ============================================================
# Usage:
#   terraform init
#   terraform apply -var="sp_site_url=https://contoso.sharepoint.com/sites/KPIs"
#
# No requirements.txt or pip pre-step needed.
# Terraform builds the dependency layer inline via null_resource.
# ============================================================

terraform {
  required_providers {
    aws  = { source = "hashicorp/aws", version = "~> 5.0" }
    null = { source = "hashicorp/null", version = "~> 3.0" }
  }

  backend "s3" {
    key     = "terraform.tfstate"
    encrypt = true
  }
}

provider "aws" {
  region = var.aws_region
}

# ---- Variables -----------------------------------------------

variable "aws_region"       { default = "us-east-1" }
variable "function_name"    { default = "sharepoint-kpi-sync" }
variable "schedule_cron"    { default = "cron(0 * * * ? *)" } # hourly

variable "sp_tenant_id"     {}
variable "sp_client_id"     {}
variable "sp_client_secret" { sensitive = true }
variable "sp_site_url"      {}
variable "sp_tenant_name"   {}

variable "sync_jobs_json" {
  description = "JSON-encoded list of sync job definitions"
  default     = "[]"
}

# ---- Locals --------------------------------------------------

locals {
  params = {
    "SP_TENANT_ID"     = var.sp_tenant_id
    "SP_CLIENT_ID"     = var.sp_client_id
    "SP_CLIENT_SECRET" = var.sp_client_secret
    "SP_SITE_URL"      = var.sp_site_url
    "SP_TENANT_NAME"   = var.sp_tenant_name
  }

  # Only the .py files that belong on Lambda.
  # azure_function.py is intentionally excluded — it's Azure Functions only.
  lambda_source_files = [
    "lambda_handler.py",
    "runner.py",
    "client.py",
    "config.py",
    "base_connector.py",
    "entraid.py",
    "meraki.py",
    "sendgrid_ninjaone.py",
    "generic.py",
  ]

  # Add/remove packages here. Changing this list forces a layer rebuild.
  # boto3   — omitted, pre-installed in the Lambda runtime.
  # azure-* — omitted, Azure Functions only.
  lambda_packages = ["requests"]

  # Changing the list above changes this hash → triggers null_resource → reruns pip.
  packages_hash = md5(join(",", sort(local.lambda_packages)))
}

# ---- IAM Role ------------------------------------------------

data "aws_iam_policy_document" "assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_role" {
  name               = "${var.function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
}

resource "aws_iam_role_policy_attachment" "basic" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "secrets_ssm" {
  statement {
    actions   = ["secretsmanager:GetSecretValue", "ssm:GetParameter"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "secrets" {
  name   = "secrets-access"
  role   = aws_iam_role.lambda_role.id
  policy = data.aws_iam_policy_document.secrets_ssm.json
}

# ---- Secrets in SSM Parameter Store --------------------------

resource "aws_ssm_parameter" "kpi_params" {
  for_each = local.params
  name     = "/sharepoint-kpi/${each.key}"
  type     = contains(["SP_CLIENT_SECRET"], each.key) ? "SecureString" : "String"
  value    = each.value
}

# ---- Lambda Package (source code only, ~100 KB) --------------
# Zips only the explicit .py file list — can never accidentally
# include venv/, site-packages/, or any other local directory.

data "archive_file" "lambda_zip" {
  type        = "zip"
  output_path = "${path.module}/lambda_package.zip"

  dynamic "source" {
    for_each = local.lambda_source_files
    content {
      filename = source.value
      content  = file("${path.module}/${source.value}")
    }
  }
}

# ---- Lambda Layer (pip dependencies) -------------------------
# pip runs inside Terraform via null_resource — no requirements.txt
# or manual pip step needed in CI or locally.
#
# Uses /tmp so there are no leftover build artifacts in the repo,
# and the GitHub Actions runner always has write access to it.
#
# The layer only rebuilds when lambda_packages changes.

resource "null_resource" "build_layer" {
  triggers = {
    packages = local.packages_hash
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -e
      rm -rf /tmp/lambda-layer
      mkdir -p /tmp/lambda-layer/python
      pip install ${join(" ", local.lambda_packages)} \
        --platform manylinux2014_x86_64 \
        --python-version 3.12 \
        --only-binary=:all: \
        --upgrade \
        -q \
        -t /tmp/lambda-layer/python
    EOT
  }
}

data "archive_file" "lambda_layer_zip" {
  type        = "zip"
  source_dir  = "/tmp/lambda-layer"
  output_path = "${path.module}/lambda_layer.zip"
  depends_on  = [null_resource.build_layer]
}

resource "aws_lambda_layer_version" "deps" {
  filename            = data.archive_file.lambda_layer_zip.output_path
  layer_name          = "${var.function_name}-deps"
  compatible_runtimes = ["python3.12"]
  source_code_hash    = data.archive_file.lambda_layer_zip.output_base64sha256
}

# ---- Lambda Function -----------------------------------------

resource "aws_lambda_function" "kpi_sync" {
  function_name = var.function_name
  role          = aws_iam_role.lambda_role.arn
  runtime       = "python3.12"
  handler       = "lambda_handler.handler"
  timeout       = 900  # 15 minutes (max)
  memory_size   = 512

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  layers = [aws_lambda_layer_version.deps.arn]

  environment {
    variables = {
      LOG_LEVEL     = "INFO"
      PARAM_PREFIX  = "/sharepoint-kpi"
      SECRET_PREFIX = "/sharepoint-kpi"
    }
  }

  depends_on = [aws_iam_role_policy_attachment.basic]
}

# ---- EventBridge (scheduled invocation) ----------------------

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${var.function_name}-schedule"
  schedule_expression = var.schedule_cron
}

resource "aws_cloudwatch_event_target" "lambda_target" {
  rule      = aws_cloudwatch_event_rule.schedule.name
  target_id = "KPISyncLambda"
  arn       = aws_lambda_function.kpi_sync.arn

  input = jsonencode({
    jobs = jsondecode(var.sync_jobs_json)
  })
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.kpi_sync.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}

# ---- Outputs -------------------------------------------------

output "lambda_arn"      { value = aws_lambda_function.kpi_sync.arn }
output "lambda_function" { value = aws_lambda_function.kpi_sync.function_name }
