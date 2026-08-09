#!/usr/bin/env python3
"""
Setup OpenWA Webhook for Adiyan
This script registers the Adiyan webhook endpoint with OpenWA
"""

import requests
import sys
import os

# Colors for output
GREEN = '\033[92m'
RED = '\033[91m'
BLUE = '\033[94m'
YELLOW = '\033[93m'
RESET = '\033[0m'

def print_header(text):
    print(f"\n{BLUE}{'='*60}{RESET}")
    print(f"{BLUE}{text}{RESET}")
    print(f"{BLUE}{'='*60}{RESET}\n")

def print_success(text):
    print(f"{GREEN}✅ {text}{RESET}")

def print_error(text):
    print(f"{RED}❌ {text}{RESET}")

def print_warning(text):
    print(f"{YELLOW}⚠️  {text}{RESET}")

def print_info(text):
    print(f"{BLUE}ℹ️  {text}{RESET}")

def get_input(prompt, default=None):
    """Get user input with optional default"""
    if default:
        user_input = input(f"{prompt} [{default}]: ").strip()
        return user_input if user_input else default
    return input(f"{prompt}: ").strip()

def register_webhook(openwa_url, api_key, session_id, webhook_url, webhook_secret):
    """Register webhook with OpenWA"""

    endpoint = f"{openwa_url}/api/sessions/{session_id}/webhooks"

    payload = {
        "url": webhook_url,
        "events": ["message.received", "message.sent"],
        "secret": webhook_secret,
        "retryCount": 3
    }

    headers = {
        'X-API-Key': api_key,
        'Content-Type': 'application/json'
    }

    print_info(f"Registering webhook at: {endpoint}")
    print_info(f"Webhook URL: {webhook_url}")
    print_info(f"Events: {', '.join(payload['events'])}")

    try:
        response = requests.post(endpoint, json=payload, headers=headers, timeout=10)

        if response.status_code in [200, 201]:
            print_success(f"Webhook registered successfully!")
            print_info(f"Response: {response.json()}")
            return True
        else:
            print_error(f"Registration failed: {response.status_code}")
            print_error(f"Response: {response.text}")
            return False

    except requests.exceptions.ConnectionError:
        print_error(f"Cannot connect to OpenWA at {openwa_url}")
        print_info("Make sure OpenWA is running: npm start (in penwa folder)")
        return False
    except Exception as e:
        print_error(f"Error: {str(e)}")
        return False

def test_adiyan_webhook(webhook_url):
    """Test if Adiyan webhook endpoint is accessible"""
    try:
        # Send test webhook
        test_payload = {
            "event": "message.received",
            "data": {
                "sessionId": "adiyan-coaching",
                "messageId": "TEST123",
                "fromNumber": "1234567890",
                "senderName": "Test User",
                "chatId": "1234567890@c.us",
                "body": "Test message",
                "timestamp": 1691234567890,
                "isGroup": False,
                "hasMedia": False
            }
        }

        response = requests.post(webhook_url, json=test_payload, timeout=5)

        if response.status_code in [200, 202]:
            print_success(f"Webhook endpoint is accessible!")
            return True
        else:
            print_warning(f"Webhook returned status {response.status_code}")
            return False

    except requests.exceptions.ConnectionError:
        print_error(f"Cannot connect to Adiyan webhook at {webhook_url}")
        print_info("Make sure Adiyan is running: python main.py")
        return False
    except Exception as e:
        print_error(f"Error testing webhook: {str(e)}")
        return False

def main():
    print_header("🔗 OpenWA Webhook Registration for Adiyan")

    # Get configuration
    print("Enter your OpenWA configuration:\n")

    openwa_url = get_input("OpenWA URL", "http://localhost:2785")
    api_key = get_input("OpenWA API Key", "")
    session_id = get_input("Session ID", "adiyan-coaching")

    print("\nEnter your Adiyan configuration:\n")

    adiyan_host = get_input("Adiyan Host", "localhost")
    adiyan_port = get_input("Adiyan Port", "5001")
    webhook_url = f"http://{adiyan_host}:{adiyan_port}/webhook/openwa"

    webhook_secret = get_input("Webhook Secret (for HMAC signing)", "adiyan-secret-key")

    # Verify inputs
    print("\n" + "="*60)
    print("Configuration Summary:")
    print("="*60)
    print(f"OpenWA URL:    {openwa_url}")
    print(f"API Key:       {api_key[:20]}..." if len(api_key) > 20 else f"API Key:       {api_key}")
    print(f"Session ID:    {session_id}")
    print(f"Webhook URL:   {webhook_url}")
    print(f"Webhook Secret: {webhook_secret}")
    print("="*60 + "\n")

    confirm = input("Proceed with registration? (y/n): ").strip().lower()
    if confirm != 'y':
        print_warning("Cancelled")
        return

    # Test Adiyan webhook first
    print("\n📋 Testing Adiyan webhook endpoint...")
    if not test_adiyan_webhook(webhook_url):
        print_error("Adiyan webhook not reachable. Is Adiyan running?")
        return

    # Register webhook
    print("\n📋 Registering webhook with OpenWA...")
    if register_webhook(openwa_url, api_key, session_id, webhook_url, webhook_secret):
        print_success("Webhook registered successfully!\n")

        print("Next steps:")
        print("1. ✅ Adiyan is running (python main.py)")
        print("2. ✅ OpenWA is running (npm start in penwa/)")
        print("3. ✅ Webhook is registered")
        print("\nNow:")
        print("• Send a WhatsApp message to your linked number")
        print("• Message should flow through Adiyan pipeline")
        print("• Response should be sent back via OpenWA to WhatsApp")
        print("\nCheck logs:")
        print("  tail -f ~/.Adiyan/orchestrator.log")
    else:
        print_error("Failed to register webhook\n")
        print("Troubleshooting:")
        print("1. Check OpenWA is running: http://localhost:2785")
        print("2. Verify API key is correct (get from Settings → API Keys)")
        print("3. Verify session ID exists (check OpenWA Sessions tab)")
        print("4. Check Adiyan is running and webhook endpoint is working")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n")
        print_warning("Cancelled")
        sys.exit(0)
