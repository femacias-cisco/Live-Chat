"""
Lambda function to process Webex file attachments and query the Circuit AI API.

Compatible with Python 3.12 on AWS Lambda.

Required Lambda Layers (public ARNs - us-east-1):
  - Pillow:    arn:aws:lambda:us-east-1:770693421928:layer:Klayers-p312-Pillow:10
  - requests:  arn:aws:lambda:us-east-1:770693421928:layer:Klayers-p312-requests:22
  - pypdf:     not available as public layer — package in a custom layer or deployment zip

Required IAM permissions:
  - ssm:GetParameters on the /webex-gcs/* parameter paths

Required SSM Parameters (/webex-gcs/):
  - webex-token
  - circuit-app-key
  - circuit-basicApi-clientId
  - circuit-basicApi-clientSecret
"""

import json
import re
import base64
import logging
from io import BytesIO

import boto3
import requests
from PIL import Image
from pypdf import PdfReader

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# SSM client — initialized at module level for connection reuse across warm invocations
ssm_client = boto3.client('ssm')

# Constants
CIRCUIT_TOKEN_URL = "https://id.cisco.com/oauth2/default/v1/token"
CIRCUIT_API_URL = "https://chat-ai.cisco.com/openai/deployments/gpt-5-nano/chat/completions"
REQUIRED_FIELDS = ['fileUrl', 'querySystem', 'queryMsg']
RESPONSE_HEADERS = {
    'Content-Type': 'application/json',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY'
}


# =============================================================================
# Handler
# =============================================================================

def lambda_handler(event: dict, context: object) -> dict:
    """
    Entry point for the Lambda function.
    Downloads a file from Webex, converts it to base64 images,
    and sends it to the Circuit API for processing.
    """
    try:
        logger.info("Lambda function started")

        body, error = parse_event_body(event)
        if error:
            return error

        credentials = get_credentials()

        download_result = download_file(body['fileUrl'], credentials['webex_token'])
        if not download_result['success']:
            logger.error(f"Download failed: {download_result['error']}")
            return build_response(500, {'error': f"Failed to download file: {download_result['error']}"})

        file_name = download_result['filename']
        if not file_name:
            logger.error("No filename found in Content-Disposition header")
            return build_response(400, {'error': 'No filename found in Content-Disposition header'})

        images, error = convert_file_to_images(download_result['content'], download_result['content_type'])
        if error:
            return error

        payload = build_circuit_payload(images, body['querySystem'], body['queryMsg'], credentials['circuit_app_key'])

        token_result = circuit_api_token(CIRCUIT_TOKEN_URL, credentials['circuit_basicApi_clientId'], credentials['circuit_basicApi_clientSecret'])
        if not token_result['success']:
            logger.error(f"Token retrieval failed: {token_result['error']}")
            return build_response(500, {'error': f"Failed to obtain Circuit token: {token_result['error']}"})

        api_headers = {
            "Content-Type": "application/json",
            "api-key": token_result['access_token']
        }

        result = circuit_api(CIRCUIT_API_URL, api_headers, payload)
        if result['success']:
            logger.info("Circuit API call successful")
            return build_response(200, {
                'message': 'File processed successfully',
                'circuitResponse': result['circuitResponse'],
                'fileName': file_name,
                'contentType': download_result['content_type'],
                'contentLength': download_result['content_length']
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


# =============================================================================
# Request parsing
# =============================================================================

def parse_event_body(event: dict) -> tuple[dict | None, dict | None]:
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


# =============================================================================
# File processing
# =============================================================================

def convert_file_to_images(file_content: bytes, content_type: str) -> tuple[list | None, dict | None]:
    """
    Converts file bytes to a list of PIL Images.
    Supports image/* and application/pdf content types.
    Returns (images, None) on success or (None, error_response) on failure.
    """
    if content_type.startswith("image/"):
        return [Image.open(BytesIO(file_content))], None
    elif content_type == "application/pdf":
        reader = PdfReader(BytesIO(file_content))
        images = []
        for page in reader.pages:
            for img_obj in page.images:
                images.append(Image.open(BytesIO(img_obj.data)))
        if not images:
            logger.error("No images found in PDF")
            return None, build_response(400, {'error': 'No images found in PDF'})
        return images, None
    else:
        logger.error(f"Unsupported file type: {content_type}")
        return None, build_response(400, {'error': f"Unsupported file type: {content_type}"})


def build_circuit_payload(images: list, query_system: str, query_msg: str, app_key: str) -> dict:
    """
    Builds the Circuit API request payload from a list of PIL images.
    Each image is base64-encoded as PNG and included as an image_url content block.
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


# =============================================================================
# HTTP helpers
# =============================================================================

def download_file(url: str, webex_token: str) -> dict:
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
        filename = extract_filename_from_content_disposition(response.headers.get('Content-Disposition', ''))
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


def circuit_api_token(token_url: str, client_id: str, client_secret: str) -> dict:
    """
    Obtains an OAuth2 client_credentials access token from the Circuit identity provider.
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
            },
            timeout=10
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


def circuit_api(api_url: str, api_headers: dict, payload: dict) -> dict:
    """
    Sends the payload to the Circuit API and returns the model response.
    Returns a dict with success and circuitResponse.
    """
    try:
        response = requests.post(api_url, headers=api_headers, json=payload, timeout=60)
        if response.status_code in [200, 201]:
            logger.info("Circuit API responded successfully")
            return {'success': True, 'circuitResponse': response.json()["choices"][0]["message"]["content"]}
        else:
            logger.error(f"Circuit API error: {response.status_code} - {response.text}")
            return {'success': False, 'error': f"HTTP {response.status_code}: {response.text}"}

    except Exception as e:
        logger.error(f"Error calling Circuit API: {str(e)}")
        return {'success': False, 'error': str(e)}


def build_response(status_code: int, body: dict) -> dict:
    """
    Builds a standard Lambda HTTP response dict.
    """
    return {
        'statusCode': status_code,
        'headers': RESPONSE_HEADERS,
        'body': json.dumps(body)
    }


# =============================================================================
# AWS helpers
# =============================================================================

def get_credentials() -> dict:
    """
    Retrieves all required credentials from AWS Systems Manager Parameter Store.
    Raises an exception if retrieval fails.
    """
    try:
        response = ssm_client.get_parameters(
            Names=[
                '/webex-gcs/webex-token',
                '/webex-gcs/circuit-app-key',
                '/webex-gcs/circuit-basicApi-clientId',
                '/webex-gcs/circuit-basicApi-clientSecret'
            ],
            WithDecryption=True
        )

        credentials = {}
        for param in response['Parameters']:
            name = param['Name']
            if name == '/webex-gcs/webex-token':
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


def extract_filename_from_content_disposition(content_disposition: str) -> str | None:
    """
    Extracts the filename value from a Content-Disposition header string.
    Returns None if no filename is found.
    """
    match = re.search(r'filename="?([^"]+)"?', content_disposition, re.IGNORECASE)
    return match.group(1) if match else None
