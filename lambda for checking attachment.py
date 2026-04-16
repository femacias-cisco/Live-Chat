import json
import boto3
import requests
import re
import base64
import logging
from io import BytesIO
from datetime import datetime
from pdf2image import convert_from_bytes

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize SSM client
ssm_client = boto3.client('ssm')

CIRCUIT_TOKEN_URL = "https://id.cisco.com/oauth2/default/v1/token"
CIRCUIT_API_URL = "https://chat-ai.cisco.com/openai/deployments/gpt-5-nano/chat/completions"
REQUIRED_FIELDS = ['fileUrl', 'querySystem', 'queryMsg']
RESPONSE_HEADERS = {
    'Content-Type': 'application/json',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY'
}


def lambda_handler(event, context):
    """
    Downloads a file from Webex, converts it to base64 images,
    and sends it to the Circuit API for processing.
    """
    try:
        logger.info("Lambda function started")

        body, error = parse_event_body(event)
        if error:
            return error

        file_url = body['fileUrl']
        query_msg = body['queryMsg']
        query_system = body['querySystem']

        credentials = get_credentials()

        download_result = download_file(file_url, credentials['webex_token'])
        if not download_result['success']:
            logger.error(f"Download failed: {download_result['error']}")
            return build_response(500, {'error': f"Failed to download file: {download_result['error']}"})

        file_content = download_result['content']
        content_type = download_result['content_type']
        content_length = download_result['content_length']
        file_name = download_result['filename']

        if not file_name:
            logger.error("No filename found in Content-Disposition header")
            return build_response(400, {'error': 'No filename found in Content-Disposition header'})

        images, error = convert_file_to_images(file_content, content_type)
        if error:
            return error

        payload = build_circuit_payload(images, query_system, query_msg, credentials['circuit_app_key'])

        circuit_token_result = circuit_api_token(CIRCUIT_TOKEN_URL, credentials['circuit_basicApi_clientId'], credentials['circuit_basicApi_clientSecret'])
        api_headers = {
            "Content-Type": "application/json",
            "api-key": circuit_token_result['access_token']
        }

        result = circuit_api(CIRCUIT_API_URL, api_headers, payload)
        if result['success']:
            logger.info("Circuit API call successful")
            return build_response(200, {
                'message': 'File processed successfully',
                'circuitResponse': result['circuitResponse'],
                'fileName': file_name,
                'contentType': content_type,
                'contentLength': content_length
            })
        else:
            logger.error(f"Circuit API call failed: {result['error']}")
            return build_response(500, {'error': f"Failed to call Circuit API: {result['error']}"})

    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {str(e)}")
        return build_response(400, {'error': 'Invalid JSON in request body'})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return build_response(500, {'error': 'Internal server error'})


def parse_event_body(event):
    """
    Parses and validates the Lambda event body.
    Returns (body, None) on success or (None, error_response) on failure.
    """
    if 'body' in event:
        body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
    else:
        body = event

    missing_fields = [f for f in REQUIRED_FIELDS if f not in body]
    if missing_fields:
        logger.error(f"Missing required fields: {missing_fields}")
        return None, build_response(400, {'error': f"Missing required fields: {', '.join(missing_fields)}"})

    if not body['fileUrl'] or not body['queryMsg']:
        logger.error("Empty fileUrl or queryMsg")
        return None, build_response(400, {'error': 'fileUrl and queryMsg cannot be empty'})

    return body, None


def convert_file_to_images(file_content, content_type):
    """
    Converts file content to a list of PIL images.
    Supports image/* and application/pdf content types.
    Returns (images, None) on success or (None, error_response) on failure.
    """
    if content_type.startswith("image/"):
        from PIL import Image
        images = [Image.open(BytesIO(file_content))]
    elif content_type == "application/pdf":
        images = convert_from_bytes(file_content, dpi=200)
    else:
        logger.error(f"Unsupported file type: {content_type}")
        return None, build_response(400, {'error': f"Unsupported file type: {content_type}"})

    return images, None


def build_circuit_payload(images, query_system, query_msg, app_key):
    """
    Builds the Circuit API payload from a list of PIL images and query parameters.
    Each image is base64-encoded as PNG and included as image_url content.
    """
    image_content = []
    for i, image in enumerate(images):
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        b64_string = base64.b64encode(buffer.getvalue()).decode("utf-8")
        logger.info(f"Page {i + 1}: encoded ({len(b64_string)} chars)")
        image_content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{b64_string}",
                "detail": "high"
            }
        })

    return {
        "messages": [
            {"role": "system", "content": query_system},
            {"role": "user", "content": [{"type": "text", "text": query_msg}, *image_content]}
        ],
        "user": json.dumps({"appkey": app_key}),
        "max_tokens": 4096
    }


def build_response(status_code, body):
    """
    Builds a standard Lambda HTTP response.
    """
    return {
        'statusCode': status_code,
        'headers': RESPONSE_HEADERS,
        'body': json.dumps(body)
    }


def download_file(url, webex_token):
    """
    Downloads a file from the given URL using a Webex Bearer token.
    Returns a dict with success, content, content_type, content_length, and filename.
    """
    try:
        headers = {
            'Authorization': f'Bearer {webex_token}',
            'User-Agent': 'AWS-Lambda-WebexConnect/1.0',
            'Accept': '*/*'
        }

        response = requests.get(url, headers=headers, timeout=30, stream=True, verify=True)
        response.raise_for_status()

        content_type = response.headers.get('Content-Type', 'application/octet-stream')
        content_length = response.headers.get('Content-Length', '0')
        content_disposition = response.headers.get('Content-Disposition', '')
        filename = extract_filename_from_content_disposition(content_disposition)

        file_content = b''.join(chunk for chunk in response.iter_content(chunk_size=8192) if chunk)

        logger.info(f"Downloaded {len(file_content)} bytes — type: {content_type}, file: {filename}")

        return {
            'success': True,
            'content': file_content,
            'content_type': content_type,
            'content_length': content_length,
            'filename': filename
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Request error: {str(e)}")
        return {'success': False, 'error': str(e)}
    except Exception as e:
        logger.error(f"Unexpected error during download: {str(e)}")
        return {'success': False, 'error': str(e)}


def circuit_api_token(token_url, client_id, client_secret):
    """
    Obtains an OAuth2 access token from the Circuit identity provider.
    Returns a dict with success and access_token.
    """
    try:
        response = requests.post(
            token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret
            }
        )
        if response.status_code in [200, 201]:
            logger.info("Circuit token obtained successfully")
            return {'success': True, 'access_token': response.json().get("access_token")}
        else:
            logger.error(f"Token generation failed: {response.status_code} - {response.text}")
            return {'success': False, 'error': f"HTTP {response.status_code}: {response.text}"}

    except Exception as e:
        logger.error(f"Error obtaining Circuit token: {str(e)}")
        return {'success': False, 'error': str(e)}


def circuit_api(api_url, api_headers, payload):
    """
    Sends the payload to the Circuit API and returns the model response.
    Returns a dict with success and circuitResponse.
    """
    try:
        response = requests.post(api_url, headers=api_headers, json=payload)
        if response.status_code in [200, 201]:
            logger.info("Circuit API responded successfully")
            return {'success': True, 'circuitResponse': response.json()["choices"][0]["message"]["content"]}
        else:
            logger.error(f"Circuit API error: {response.status_code} - {response.text}")
            return {'success': False, 'error': f"HTTP {response.status_code}: {response.text}"}

    except Exception as e:
        logger.error(f"Error calling Circuit API: {str(e)}")
        return {'success': False, 'error': str(e)}


def get_credentials():
    """
    Retrieves all credentials from AWS Systems Manager Parameter Store.
    """
    try:
        response = ssm_client.get_parameters(
            Names=[
                '/webex-gcs/access-key',
                '/webex-gcs/secret-key',
                '/webex-gcs/circuit-app-key',
                '/webex-gcs/webex-token',
                '/webex-gcs/circuit-basicApi-clientId',
                '/webex-gcs/circuit-basicApi-clientSecret'
            ],
            WithDecryption=True
        )

        credentials = {}
        for param in response['Parameters']:
            name = param['Name']
            if name == '/webex-gcs/access-key':
                credentials['gcs_access_key'] = param['Value']
            elif name == '/webex-gcs/secret-key':
                credentials['gcs_secret_key'] = param['Value']
            elif name == '/webex-gcs/webex-token':
                credentials['webex_token'] = param['Value']
            elif name == '/webex-gcs/circuit-app-key':
                credentials['circuit_app_key'] = param['Value']
            elif name == '/webex-gcs/circuit-basicApi-clientId':
                credentials['circuit_basicApi_clientId'] = param['Value']
            elif name == '/webex-gcs/circuit-basicApi-clientSecret':
                credentials['circuit_basicApi_clientSecret'] = param['Value']

        logger.info(f"Retrieved {len(credentials)} credentials from Parameter Store")
        return credentials

    except Exception as e:
        logger.error(f"Error retrieving credentials: {str(e)}")
        raise


def extract_filename_from_content_disposition(content_disposition):
    """
    Extracts the filename from a Content-Disposition header value.
    """
    match = re.search(r'filename="?([^"]+)"?', content_disposition, re.IGNORECASE)
    return match.group(1) if match else None
