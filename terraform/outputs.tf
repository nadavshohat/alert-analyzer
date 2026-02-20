output "ecr_repository_url" {
  description = "ECR repository URL"
  value       = aws_ecr_repository.alert_analyzer.repository_url
}

output "iam_role_arn" {
  description = "IAM role ARN for IRSA"
  value       = aws_iam_role.alert_analyzer.arn
}
