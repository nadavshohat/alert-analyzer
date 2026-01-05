#!/usr/bin/env python3
"""
Alert Analyzer - AI-powered root cause analysis for Kubernetes alerts.
Receives webhooks from Groundcover Keep, fetches logs, analyzes with Bedrock Claude.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import clickhouse_connect
import boto3
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration from environment
CLICKHOUSE_HOST = os.environ.get('CLICKHOUSE_HOST', 'groundcover-clickhouse')
CLICKHOUSE_PORT = int(os.environ.get('CLICKHOUSE_PORT', '8123'))
CLICKHOUSE_USER = os.environ.get('CLICKHOUSE_USER', 'default')
CLICKHOUSE_PASSWORD = os.environ.get('CLICKHOUSE_PASSWORD', '')
CLICKHOUSE_DATABASE = os.environ.get('CLICKHOUSE_DATABASE', 'groundcover')
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')
AWS_REGION = os.environ.get('AWS_REGION', 'us-west-2')
BEDROCK_MODEL_ID = os.environ.get('BEDROCK_MODEL_ID', 'anthropic.claude-sonnet-4-20250514-v1:0')
LOG_LOOKBACK_MINUTES = int(os.environ.get('LOG_LOOKBACK_MINUTES', '30'))


def get_clickhouse_client():
    """Create ClickHouse client connection."""
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DATABASE
    )


def fetch_logs(namespace: str, workload: str, pod: str = None, minutes: int = None) -> list:
    """Fetch recent logs from ClickHouse for the given workload."""
    if minutes is None:
        minutes = LOG_LOOKBACK_MINUTES

    try:
        client = get_clickhouse_client()

        # Build query based on available filters
        conditions = [f"timestamp >= now() - INTERVAL {minutes} MINUTE"]

        if namespace:
            conditions.append(f"namespace = '{namespace}'")
        if workload:
            conditions.append(f"workload = '{workload}'")
        if pod:
            conditions.append(f"pod = '{pod}'")

        where_clause = " AND ".join(conditions)

        query = f"""
        SELECT
            timestamp,
            namespace,
            workload,
            pod,
            container,
            level,
            content
        FROM logs
        WHERE {where_clause}
        ORDER BY timestamp DESC
        LIMIT 200
        """

        logger.info(f"Executing ClickHouse query: {query}")
        result = client.query(query)

        logs = []
        for row in result.result_rows:
            logs.append({
                'timestamp': str(row[0]),
                'namespace': row[1],
                'workload': row[2],
                'pod': row[3],
                'container': row[4],
                'level': row[5],
                'content': row[6]
            })

        logger.info(f"Fetched {len(logs)} log entries")
        return logs

    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        return []


def analyze_with_bedrock(alert_data: dict, logs: list) -> str:
    """Send alert and logs to Bedrock Claude for analysis."""
    try:
        bedrock = boto3.client('bedrock-runtime', region_name=AWS_REGION)

        # Format logs for the prompt
        log_text = "\n".join([
            f"[{log['timestamp']}] [{log['level']}] {log['container']}: {log['content']}"
            for log in logs[:100]  # Limit to avoid token limits
        ])

        if not log_text:
            log_text = "No logs found for this time period."

        prompt = f"""You are a Kubernetes expert analyzing a production incident.
An alert has fired and you need to determine the ROOT CAUSE by analyzing the logs.

## Alert Information
- Title: {alert_data.get('title', 'Unknown')}
- Severity: {alert_data.get('severity', 'Unknown')}
- Namespace: {alert_data.get('namespace', 'Unknown')}
- Workload: {alert_data.get('workload', 'Unknown')}
- Description: {alert_data.get('description', 'No description')}

## Recent Logs (most recent first)
```
{log_text}
```

## Your Task
1. Identify the ROOT CAUSE of this issue from the logs
2. Explain WHY this happened (not just what happened)
3. Provide specific, actionable recommendations to fix it

Be concise but thorough. Focus on the actual error, not generic advice.
If the logs don't contain enough information, say so clearly.
"""

        # Use Converse API
        response = bedrock.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [{"text": prompt}]
                }
            ],
            inferenceConfig={
                "maxTokens": 1024,
                "temperature": 0.3
            }
        )

        # Extract response text
        output_message = response.get('output', {}).get('message', {})
        content_blocks = output_message.get('content', [])

        analysis = ""
        for block in content_blocks:
            if 'text' in block:
                analysis += block['text']

        logger.info("Bedrock analysis completed successfully")
        return analysis

    except Exception as e:
        logger.error(f"Error calling Bedrock: {e}")
        return f"Error analyzing with AI: {str(e)}"


def send_to_slack(alert_data: dict, analysis: str):
    """Send enriched alert to Slack."""
    if not SLACK_WEBHOOK_URL:
        logger.warning("No Slack webhook configured, skipping notification")
        return False

    try:
        # Determine emoji based on severity
        severity = alert_data.get('severity', 'info').lower()
        emoji_map = {
            'critical': ':rotating_light:',
            'error': ':x:',
            'warning': ':warning:',
            'info': ':information_source:'
        }
        emoji = emoji_map.get(severity, ':bell:')

        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{emoji} {alert_data.get('title', 'Alert')}",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Namespace:*\n{alert_data.get('namespace', 'N/A')}"},
                        {"type": "mrkdwn", "text": f"*Workload:*\n{alert_data.get('workload', 'N/A')}"},
                        {"type": "mrkdwn", "text": f"*Severity:*\n{severity.upper()}"},
                        {"type": "mrkdwn", "text": f"*Time:*\n{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"}
                    ]
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*:robot_face: AI Root Cause Analysis:*\n\n{analysis[:2900]}"  # Slack limit
                    }
                }
            ]
        }

        response = requests.post(
            SLACK_WEBHOOK_URL,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=10
        )

        if response.status_code == 200:
            logger.info("Successfully sent to Slack")
            return True
        else:
            logger.error(f"Slack returned status {response.status_code}: {response.text}")
            return False

    except Exception as e:
        logger.error(f"Error sending to Slack: {e}")
        return False


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy"})


@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive alert webhook from Keep."""
    try:
        data = request.get_json()
        logger.info(f"Received webhook: {json.dumps(data, default=str)[:500]}")

        # Extract alert information from Keep webhook
        # Keep sends various formats, handle common ones
        alert_data = {
            'title': data.get('title') or data.get('name') or data.get('alert', {}).get('title', 'Unknown Alert'),
            'severity': data.get('severity') or data.get('status') or 'info',
            'namespace': data.get('namespace') or data.get('labels', {}).get('namespace', ''),
            'workload': data.get('workload') or data.get('labels', {}).get('workload', ''),
            'pod': data.get('pod') or data.get('labels', {}).get('pod', ''),
            'description': data.get('description') or data.get('message') or ''
        }

        # Try to extract from nested structures if not found
        if not alert_data['namespace'] and 'alert' in data:
            alert_data['namespace'] = data['alert'].get('namespace', '')
            alert_data['workload'] = data['alert'].get('workload', '')

        logger.info(f"Parsed alert data: {alert_data}")

        # Fetch relevant logs
        logs = fetch_logs(
            namespace=alert_data['namespace'],
            workload=alert_data['workload'],
            pod=alert_data['pod']
        )

        # Analyze with Bedrock
        analysis = analyze_with_bedrock(alert_data, logs)

        # Send to Slack
        slack_sent = send_to_slack(alert_data, analysis)

        return jsonify({
            "status": "processed",
            "logs_found": len(logs),
            "slack_sent": slack_sent
        })

    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/test', methods=['POST'])
def test_analysis():
    """Test endpoint to manually trigger analysis."""
    try:
        data = request.get_json()
        namespace = data.get('namespace', '')
        workload = data.get('workload', '')

        if not namespace or not workload:
            return jsonify({"error": "namespace and workload required"}), 400

        alert_data = {
            'title': f'Test Alert: {workload}',
            'severity': 'info',
            'namespace': namespace,
            'workload': workload,
            'pod': '',
            'description': 'Manual test analysis'
        }

        logs = fetch_logs(namespace=namespace, workload=workload)
        analysis = analyze_with_bedrock(alert_data, logs)
        send_to_slack(alert_data, analysis)

        return jsonify({
            "status": "success",
            "logs_found": len(logs),
            "analysis": analysis
        })

    except Exception as e:
        logger.error(f"Error in test: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8080'))
    app.run(host='0.0.0.0', port=port)
