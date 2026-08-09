const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode');
const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');
const amqp = require('amqplib');
const { v4: uuidv4 } = require('uuid');

const app = express();
app.use(cors());
app.use(express.json());

const PORT = 3001;
const AUTH_PATH = path.join(process.env.HOME, '.Adiyan', 'whatsapp_auth');
const WHITELIST_PATH = path.join(process.env.HOME, '.Adiyan', 'whitelist.txt');
const RABBITMQ_URL = process.env.RABBITMQ_URL || 'amqp://guest:guest@localhost/';

let client = null;
let qrCodeData = null;
let isConnected = false;
let amqpConnection = null;
let amqpChannel = null;
let whitelist = new Set();

// Store pending message responses
const pendingResponses = new Map();

// Load whitelist from file
function loadWhitelist() {
  try {
    if (fs.existsSync(WHITELIST_PATH)) {
      const content = fs.readFileSync(WHITELIST_PATH, 'utf-8');
      whitelist = new Set(
        content
          .split('\n')
          .map(line => line.trim())
          .filter(line => line.length > 0)
      );
      console.log(`📋 Whitelist loaded: ${whitelist.size} contacts`);
    } else {
      console.log(`⚠️  Whitelist file not found: ${WHITELIST_PATH}`);
      whitelist = new Set();
    }
  } catch (err) {
    console.error(`❌ Failed to load whitelist: ${err.message}`);
    whitelist = new Set();
  }
}

// Check if contact is whitelisted
function isWhitelisted(contactName) {
  return whitelist.has(contactName);
}

console.log('🚀 Starting WhatsApp Web Service...');
console.log(`📁 Auth path: ${AUTH_PATH}`);
console.log(`📨 RabbitMQ: ${RABBITMQ_URL}`);

// Setup RabbitMQ connection
async function setupRabbitMQ() {
  try {
    amqpConnection = await amqp.connect(RABBITMQ_URL);
    amqpChannel = await amqpConnection.createChannel();

    // Declare exchange
    await amqpChannel.assertExchange('adiyan', 'topic', { durable: true });

    // Declare queues
    await amqpChannel.assertQueue('messages-incoming', { durable: true });
    await amqpChannel.bindQueue('messages-incoming', 'adiyan', 'messages.incoming');

    // Response queue for this service
    const responseQueue = await amqpChannel.assertQueue('whatsapp-responses', {
      durable: true
    });

    // Listen for responses from orchestrator
    await amqpChannel.consume(responseQueue.queue, async (msg) => {
      if (msg) {
        try {
          const responseData = JSON.parse(msg.content.toString());
          const { message_id, contact_name, response } = responseData;

          console.log(`📬 Received response for ${message_id}`);

          // Send to waiting request
          if (pendingResponses.has(message_id)) {
            const { res: httpRes, contact } = pendingResponses.get(message_id);

            // Send to WhatsApp
            try {
              console.log(`📤 Sending to WhatsApp: contact="${contact}", message preview="${response.substring(0, 50)}..."`);
              await sendToWhatsApp(contact, response);
              console.log(`✅ Sent to WhatsApp: ${contact}`);

              httpRes.json({
                success: true,
                contact_name: contact,
                response_sent: true
              });
            } catch (err) {
              console.error(`❌ Failed to send to WhatsApp: ${err.message}`);
              console.error(`   Contact: ${contact}`);
              console.error(`   Connected: ${isConnected}`);
              httpRes.status(500).json({
                success: false,
                error: err.message
              });
            }

            pendingResponses.delete(message_id);
          }
        } catch (err) {
          console.error(`❌ Error processing response: ${err.message}`);
        }

        amqpChannel.ack(msg);
      }
    });

    console.log('✅ RabbitMQ connected');
    return true;
  } catch (err) {
    console.error('❌ RabbitMQ connection failed:', err.message);
    return false;
  }
}

// Send message to WhatsApp
async function sendToWhatsApp(contactName, message) {
  if (!isConnected || !client) {
    throw new Error('WhatsApp not connected');
  }

  const chats = await client.getChats();
  console.log(`🔍 Looking for contact: "${contactName}" in ${chats.length} chats`);

  let targetChat = null;

  for (const chat of chats) {
    const chatName = chat.name || (chat.contact && chat.contact.name) || chat.id;
    console.log(`   Chat: "${chatName}" (id: ${chat.id._serialized})`);
    if (chatName === contactName) {
      targetChat = chat;
      break;
    }
  }

  if (!targetChat) {
    throw new Error(`Contact '${contactName}' not found in ${chats.length} chats`);
  }

  console.log(`✅ Found chat, sending message...`);
  await client.sendMessage(targetChat.id._serialized, message);
}

// Initialize services
async function initServices() {
  loadWhitelist();
  await setupRabbitMQ();
  initWhatsApp();

  // Reload whitelist every 30 seconds in case it changes
  setInterval(loadWhitelist, 30000);
}

// Initialize WhatsApp client
function initWhatsApp() {
  client = new Client({
    authStrategy: new LocalAuth({ clientId: 'adiyan', dataPath: AUTH_PATH }),
    puppeteer: {
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox']
    }
  });

  client.on('qr', (qr) => {
    console.log('📱 QR Code received');
    qrCodeData = qr;
    isConnected = false;
  });

  client.on('authenticated', () => {
    console.log('✅ WhatsApp authenticated');
  });

  client.on('ready', () => {
    console.log('✅ WhatsApp client ready');
    isConnected = true;
    qrCodeData = null;
  });

  client.on('disconnected', () => {
    console.log('❌ WhatsApp disconnected');
    isConnected = false;
    qrCodeData = null;
    setTimeout(() => {
      console.log('🔄 Attempting to reconnect...');
      initWhatsApp();
    }, 5000);
  });

  // Handle incoming messages
  client.on('message', async (msg) => {
    try {
      const contact = await msg.getContact();
      const contactName = contact.name || contact.pushname || contact.number || msg.from;

      console.log(`📨 Message from ${contactName}: ${msg.body.substring(0, 50)}...`);

      // CHECK WHITELIST FIRST
      if (!isWhitelisted(contactName)) {
        console.log(`❌ Not whitelisted: ${contactName} - ignoring`);
        return;
      }

      console.log(`✅ Whitelisted: ${contactName}`);

      // Create message ID
      const messageId = uuidv4();

      // Publish to orchestrator
      if (amqpChannel) {
        const msgData = {
          message_id: messageId,
          contact_name: contactName,
          lid: contactName,
          message_body: msg.body,
          is_user_message: true
        };

        amqpChannel.publish(
          'adiyan',
          'messages.incoming',
          Buffer.from(JSON.stringify(msgData)),
          { persistent: true }
        );

        console.log(`📤 Published to RabbitMQ (ID: ${messageId})`);
      }
    } catch (err) {
      console.error(`❌ Error processing message: ${err.message}`);
    }
  });

  client.initialize();
}

// API Endpoints

// Handle incoming message (HOLDS REQUEST OPEN)
app.post('/api/message', async (req, res) => {
  const { contact_name, message } = req.body;

  if (!contact_name || !message) {
    return res.status(400).json({ error: 'Missing contact_name or message' });
  }

  const messageId = uuidv4();
  console.log(`🔄 Holding request for ${contact_name} (ID: ${messageId})`);

  // Store the response handler with 30 second timeout
  const timeout = setTimeout(() => {
    if (pendingResponses.has(messageId)) {
      pendingResponses.delete(messageId);
      res.status(504).json({ error: 'Response timeout' });
    }
  }, 30000);

  pendingResponses.set(messageId, { res, contact: contact_name, timeout });

  // Publish to orchestrator
  if (amqpChannel) {
    const msgData = {
      message_id: messageId,
      contact_name: contact_name,
      lid: contact_name,
      message_body: message,
      is_user_message: true
    };

    amqpChannel.publish(
      'adiyan',
      'messages.incoming',
      Buffer.from(JSON.stringify(msgData)),
      { persistent: true }
    );

    console.log(`📤 Published (waiting for response)...`);
  } else {
    pendingResponses.delete(messageId);
    return res.status(503).json({ error: 'RabbitMQ not connected' });
  }
  // Request is now held open...
});

// Get QR code
app.get('/api/qr', async (req, res) => {
  if (isConnected) {
    return res.json({ connected: true, qr: null });
  }

  if (!qrCodeData) {
    return res.json({ connected: false, qr: null, message: 'Initializing...' });
  }

  try {
    const qrImage = await qrcode.toDataURL(qrCodeData);
    res.json({ connected: false, qr: qrImage });
  } catch (err) {
    console.error('QR generation error:', err);
    res.status(500).json({ error: 'Failed to generate QR' });
  }
});

// Get status
app.get('/api/status', (req, res) => {
  res.json({
    connected: isConnected,
    hasQR: !!qrCodeData,
    pendingMessages: pendingResponses.size,
    timestamp: new Date().toISOString()
  });
});

// Health check
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', service: 'WhatsApp Web Service' });
});

// Start server
app.listen(PORT, () => {
  console.log(`🌐 WhatsApp service listening on port ${PORT}`);
});

// Initialize services
initServices();

// Graceful shutdown
process.on('SIGINT', async () => {
  console.log('\n👋 Shutting down...');
  if (client) {
    await client.destroy();
  }
  if (amqpConnection) {
    await amqpConnection.close();
  }
  process.exit(0);
});
