#!/usr/bin/env node

/**
 * Adiyan OpenWA POC Test
 * Interact with OpenWA to:
 * 1. Get messages from WhatsApp (via OpenWA)
 * 2. Log message details
 * 3. Send response via OpenWA
 */

const axios = require('axios');
const fs = require('fs');
const path = require('path');

// Configuration
const OPENWA_URL = 'http://localhost:2785';
const OPENWA_API_KEY = process.env.OPENWA_API_KEY || '';
if (!OPENWA_API_KEY) {
    console.error('❌ Set OPENWA_API_KEY environment variable before running this script.');
    process.exit(1);
}
const SESSION_ID = 'ce35d62b-3342-4d61-a05b-288f6ec4fe0f'; // Use the actual session UUID
const TARGET_NAME = process.argv[2] || 'Sripriya'; // Get from CLI or default (contact name)
const LOG_FILE = path.join(process.env.HOME, '.Adiyan', 'poc_test.log');
const POLL_INTERVAL = 3000; // Poll every 3 seconds

// Ensure log directory exists
if (!fs.existsSync(path.dirname(LOG_FILE))) {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
}

// Setup logging
function log(message, level = 'INFO') {
    const timestamp = new Date().toISOString();
    const logMessage = `[${timestamp}] [${level}] ${message}`;
    console.log(logMessage);
    fs.appendFileSync(LOG_FILE, logMessage + '\n');
}

// Initialize axios with OpenWA
const openwa = axios.create({
    baseURL: OPENWA_URL,
    headers: {
        'X-API-Key': OPENWA_API_KEY,
        'Content-Type': 'application/json'
    },
    timeout: 10000
});

log('═'.repeat(70));
log('🚀 Adiyan OpenWA POC Test Started');
log('═'.repeat(70));
log(`📍 OpenWA URL: ${OPENWA_URL}`);
log(`📱 Session: ${SESSION_ID}`);
log(`🎯 Target Contact: ${TARGET_NAME}`);
log(`📝 Log File: ${LOG_FILE}`);
log('');

// Track processed message IDs to avoid duplicates
const processedMessageIds = new Set();
let messageCount = 0;
const startTimestamp = Math.floor(Date.now() / 1000);

// ============= GET CHATS & MESSAGES =============

async function getMessagesFromOpenWA() {
    try {
        log('🔍 Fetching chats from OpenWA...');

        // Get all chats for the session
        const response = await openwa.get(`/api/sessions/${SESSION_ID}/chats`);
        const chats = response.data;

        log(`📋 Retrieved ${chats.length} chat(s)`);

        // Find chat for target contact name
        let targetChat = null;
        for (const chat of chats) {
            log(`   Checking: ${chat.name || 'Unknown'} (${chat.id})`);

            if (chat.name && chat.name.toLowerCase() === TARGET_NAME.toLowerCase()) {
                targetChat = chat;
                log(`   ✅ Found target chat!`);
                break;
            }
        }

        if (!targetChat) {
            log(`⚠️  No chat found for contact "${TARGET_NAME}"`, 'WARNING');
            log(`   Available contacts: ${chats.map(c => c.name || 'Unknown').join(', ')}`);
            return null;
        }

        log('');
        log('📬 Fetching messages...');

        // Get messages from the chat
        const messagesResponse = await openwa.get(
            `/api/sessions/${SESSION_ID}/messages`,
            { params: {
                chatId: targetChat.id,
                limit: 10
            }}
        );

        const messages = messagesResponse.data.messages || messagesResponse.data || [];
        log(`   Found ${messages.length} message(s)`);

        return { chat: targetChat, messages };

    } catch (error) {
        if (error.response?.status === 404) {
            log('⚠️  Chat or session not found. Make sure:', 'WARNING');
            log('   1. OpenWA is running (http://localhost:2785)');
            log('   2. Session "executive-coach" exists');
            log('   3. WhatsApp is authenticated');
        } else {
            log(`❌ Error fetching messages: ${error.message}`, 'ERROR');
            if (error.response?.data) {
                log(`   Response: ${JSON.stringify(error.response.data)}`, 'ERROR');
            }
        }
        return null;
    }
}

// ============= SEND MESSAGE VIA OPENWA =============

async function sendMessageViaOpenWA(chatId, messageText) {
    try {
        log('📤 Sending message via OpenWA...');

        const response = await openwa.post(
            `/api/sessions/${SESSION_ID}/messages/send-text`,
            {
                chatId: chatId,
                text: messageText
            }
        );

        const messageId = response.data.messageId;
        log(`✅ Message sent! ID: ${messageId}`);
        return true;

    } catch (error) {
        log(`❌ Failed to send message: ${error.message}`, 'ERROR');
        if (error.response?.data) {
            log(`   Response: ${JSON.stringify(error.response.data)}`, 'ERROR');
        }
        return false;
    }
}

// ============= MAIN POLLING LOOP =============

async function pollAndProcess() {
    const result = await getMessagesFromOpenWA();

    if (!result) {
        log('⚠️  Waiting for chat to appear...', 'WARNING');
        return;
    }

    const { chat, messages } = result;

    // Process new messages
    for (const msg of messages) {
        // Skip if we've already processed this message
        if (processedMessageIds.has(msg.id)) {
            continue;
        }

        // Only process incoming messages received after this script started
        if (msg.direction !== 'incoming' || msg.timestamp < startTimestamp) {
            continue;
        }

        messageCount++;
        processedMessageIds.add(msg.id);

        log('');
        log('━'.repeat(70));
        log(`📨 MESSAGE #${messageCount} RECEIVED`);
        log('━'.repeat(70));

        // Extract message details
        log('📋 Message Details:');
        log(`   ID: ${msg.id}`);
        log(`   From: ${msg.from}`);
        log(`   Phone: ${msg.from.split('@')[0]}`);
        log(`   Body: "${msg.body}"`);
        log(`   Type: ${msg.type || 'text'}`);
        log(`   Time: ${new Date(msg.timestamp * 1000).toISOString()}`);
        log(`   Sender Name: ${msg.chatName || 'Unknown'}`);

        log('');
        log('✅ Message data extracted successfully');
        log('');

        // Send response via OpenWA
        const responseText = `✅ POC Test Response from Adiyan!

📨 Your message was received and processed successfully.

Details captured:
• Phone: ${msg.from.split('@')[0]}
• Sender: ${msg.chatName || 'Unknown'}
• Message: "${msg.body}"
• Time: ${new Date(msg.timestamp * 1000).toLocaleString()}

This is OpenWA API integration test. ✨`;

        const success = await sendMessageViaOpenWA(chat.id, responseText);

        if (success) {
            log('');
            log('━'.repeat(70));
            log('📊 POC VALIDATION SUMMARY');
            log('━'.repeat(70));
            log(`✅ Step 1: Sent WhatsApp message from phone (${msg.from})`);
            log(`✅ Step 2: Got message details from OpenWA API`);
            log(`✅ Step 3: Sent response via OpenWA API`);
            log('');
            log('🎉 POC Test Successful!');
            log('');
            log('Next: Connect Adiyan orchestrator to send AI responses');
            log('━'.repeat(70));
            log('');
        }
    }
}

// ============= INITIALIZATION =============

log('⏳ Starting message polling...');
log(`   Polling interval: ${POLL_INTERVAL}ms`);
log(`   Target contact: ${TARGET_NAME}`);
log('');
log('Waiting for WhatsApp message...');
log('Send a message from your phone now!');
log('');

// Start polling
let isRunning = true;
const pollInterval = setInterval(async () => {
    if (isRunning) {
        try {
            await pollAndProcess();
        } catch (error) {
            log(`Polling error: ${error.message}`, 'ERROR');
        }
    }
}, POLL_INTERVAL);

// Graceful shutdown
process.on('SIGINT', () => {
    log('\n👋 Shutting down gracefully...');
    isRunning = false;
    clearInterval(pollInterval);
    log('✅ Polling stopped');
    process.exit(0);
});

log('Press Ctrl+C to stop');
