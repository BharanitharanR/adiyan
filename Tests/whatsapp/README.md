# Adiyan WhatsApp POC Test

**3-Step Validation Test:**
1. ✅ Send message from WhatsApp
2. ✅ Capture message details in Node.js
3. ✅ Send response back via WhatsApp

---

## Quick Start

### Terminal 1: Start POC Test
```bash
cd /Users/bharani/Desktop/aiAgentCompaction/Adiyan/Tests/whatsapp
node poc_test.js
```

**Expected Output:**
```
🚀 Adiyan WhatsApp POC Test Started
🔄 Initializing WhatsApp client...
⏳ Waiting for QR code to appear...
(This will take 10-20 seconds on first run)
```

### Terminal 2: Scan QR Code (on first run)
When QR appears in Terminal 1:
1. Open WhatsApp on your phone
2. Go to **Settings** → **Linked Devices** → **Link a Device**
3. Scan the QR code from Terminal 1

Wait for message:
```
✅ WhatsApp authenticated successfully
✅ WhatsApp client ready - waiting for messages
```

### Terminal 3: Send Test Message
From your WhatsApp phone, send any message to the linked number:
```
Test message for POC
```

**Check Terminal 1 for output:**
```
📨 MESSAGE #1 RECEIVED
📋 Extracting message details...
   Message ID: ...
   From: 919080089081@c.us
   Body: "Test message for POC"
   ...

✅ Message data extracted successfully

📤 Sending response via WhatsApp...
✅ Response sent successfully!

🎉 POC Test Successful!
```

**Check WhatsApp phone:**
You should see bot's reply:
```
✅ POC Test Response from Adiyan!

📨 Your message was received and processed successfully.
...
```

---

## What Gets Tested

| Step | Operation | Validates |
|------|-----------|-----------|
| 1️⃣ | Send WhatsApp message | User can send message |
| 2️⃣ | Capture message details | Node.js receives message in real-time |
| 2️⃣ | Extract contact info | Can get contact name, phone number |
| 2️⃣ | Extract chat info | Can identify chat type (individual/group) |
| 3️⃣ | Send response | Can reply to user via WhatsApp |

---

## Output Files

**Logs are saved to:** `~/.Adiyan/poc_test.log`

View logs:
```bash
tail -f ~/.Adiyan/poc_test.log
```

**WhatsApp session data:** `~/.Adiyan/poc_auth/`

To reset WhatsApp connection (rescan QR):
```bash
rm -rf ~/.Adiyan/poc_auth/
node poc_test.js  # Will show new QR code
```

---

## Troubleshooting

### "QR Code received" but nothing happens
- Make sure WhatsApp is open on your phone
- Go to Settings → Linked Devices
- Scan the QR code from the terminal

### "Message received but response didn't send"
Check the logs:
```bash
tail -50 ~/.Adiyan/poc_test.log | grep -i error
```

### Browser crashed or frozen
Kill the process and restart:
```bash
pkill -9 -f "poc_test"
node poc_test.js
```

### Connection lost
Process will exit automatically. Restart:
```bash
node poc_test.js
```

---

## Next Steps

Once POC validation completes:

1. **Connect to Adiyan orchestrator** (see NEXT_STEPS.md)
2. **Test full E2E flow** (message → orchestrator → response)
3. **Add error handling** (connection failures, timeouts)
4. **Integrate with production** (replace with full orchestrator)

---

## Expected Latency

| Operation | Time |
|-----------|------|
| Message receipt | <100ms (real-time event) |
| Contact lookup | 20-50ms |
| Response send | 200-500ms |
| **Total E2E** | ~300-600ms |

---

## Files

- `poc_test.js` - Main POC test
- `README.md` - This file
- `package.json` - Dependencies (optional, if running standalone)

---

**Status**: ✅ Ready to test!

Run `node poc_test.js` now! 🚀
