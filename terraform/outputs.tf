output "iam_role_arn" {
  description = "IAM role ARN for IRSA"
  value       = aws_iam_role.alert_analyzer.arn
}
