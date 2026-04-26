# ============================================================
# Terraform – AWS Lambda deployment for SharePoint KPI Sync
# ============================================================
# Usage:
#   terraform init
#   terraform apply -var="sp_site_url=https://contoso.sharepoint.com/sites/KPIs"
#
# Secrets are stored in AWS Secrets Manager and SSM Parameter Store.
# The Lambda reads them at runtime via core.config.Config.
# ============================================================

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
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
  description = "JSON-encoded list of sync job definitions (same structure as sample_event.json)"
  default     = "[]"
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

locals {
  params = {
    "SP_TENANT_ID"     = var.sp_tenant_id
    "SP_CLIENT_ID"     = var.sp_client_id
    "SP_CLIENT_SECRET" = var.sp_client_secret
    "SP_SITE_URL"      = var.sp_site_url
    "SP_TENANT_NAME"   = var.sp_tenant_name
  }

  # The Python source files that belong in the Lambda zip.
  # azure_function.py is intentionally excluded — it's Azure-only.
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
}

resource "aws_ssm_parameter" "kpi_params" {
  for_each = local.params
  name     = "/sharepoint-kpi/${each.key}"
  type     = contains(["SP_CLIENT_SECRET"], each.key) ? "SecureString" : "String"
  value    = each.value
}

# ---- Lambda Package (source code only, ~100 KB) --------------
# We zip only the explicit list of .py files above.
# This completely avoids accidentally including venv/, site-packages/,
# node_modules/, or any other local dependency directory.

data "archive_file" "lambda_zip" {
  type        = "zip"
  output_path = "${path.module}/lambda_package.zip"
  excludes    = [
    ".git",
    ".github",
    ".gitignore",
    "__pycache__",
    "*.pyc",
    ".env",
    "main.tf",
    "backend.hcl",
    "*.zip",
    "LICENSE",
    "README.md",
    "sample_event.json",
  ]
  dynamic "source" {
    for_each = local.lambda_source_files
    content {
      filename = source.value
      content  = file("${path.module}/${source.value}")
    }
  }
}

# ---- Lambda Layer (pip dependencies) -------------------------
# Build the layer locally before running terraform apply:
#
#   mkdir -p layer/python
#   pip install requests \
#       --platform manylinux2014_x86_64 \
#       --python-version 3.12 \
#       --only-binary=:all: \
#       --upgrade \
#       -t layer/python
#
# boto3 is intentionally omitted — it is pre-installed in the Lambda runtime.
# azure-functions / azure-identity are intentionally omitted — AWS-only deploy.
#
# Re-run the pip command and re-apply whenever requirements change.

data "archive_file" "lambda_layer_zip" {
  type        = "zip"
  source_dir  = "${path.module}/layer"   # must contain a /python sub-directory
  output_path = "${path.module}/lambda_layer.zip"
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
