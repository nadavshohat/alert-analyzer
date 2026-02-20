# IRSA role for Bedrock access
resource "aws_iam_role" "alert_analyzer" {
  name = "alert-analyzer-${var.cluster_name}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = local.oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${local.oidc_provider}:sub" = "system:serviceaccount:${var.namespace}:alert-analyzer"
            "${local.oidc_provider}:aud" = "sts.amazonaws.com"
          }
        }
      }
    ]
  })
}

locals {
  # Strip us./global. prefix for foundation model ARN
  foundation_model_id = replace(replace(var.bedrock_model, "/^us\\./", ""), "/^global\\./", "")
}

resource "aws_iam_role_policy" "bedrock" {
  name = "bedrock-access"
  role = aws_iam_role.alert_analyzer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:${var.bedrock_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model}"
        ]
      },
      {
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:us-east-1::foundation-model/${local.foundation_model_id}",
          "arn:aws:bedrock:us-east-2::foundation-model/${local.foundation_model_id}",
          "arn:aws:bedrock:us-west-2::foundation-model/${local.foundation_model_id}"
        ]
        Condition = {
          StringLike = {
            "bedrock:InferenceProfileArn" = "arn:aws:bedrock:${var.bedrock_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model}"
          }
        }
      }
    ]
  })
}
