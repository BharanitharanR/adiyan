# POC Test - Quick Start (2 Minutes)

## Run It

```bash
cd /Users/bharani/Desktop/aiAgentCompaction/Adiyan/Tests/whatsapp
chmod +x run.sh
./run.sh
```

Or simply:
```bash
node poc_test.js
```

## What You'll See

**Terminal Output:**
```
🚀 Adiyan WhatsApp POC Test Started
✅ Node.js version: v22.x.x
📦 Installing dependencies...
✅ Dependencies installed

🚀 Starting POC Test...
🔄 Initializing WhatsApp client...
⏳ Waiting for QR code to appear...
```

**Then: A QR Code** (ASCII format in terminal)

## Scan It

1. Open **WhatsApp** on your phone
2. Tap **Settings** → **Linked Devices** → **Link a Device**
3. Point phone camera at the QR code in terminal
4. Scan

## Send Test Message

From your WhatsApp phone, send any message to the linked number:
```
Test message
```

## Result

**In Terminal:** Message details printed
```
📨 MESSAGE #1 RECEIVED
📋 Extracting message details...
   Contact: Your Name
   Phone: 919080089081
   Body: "Test message"
✅ Response sent successfully!
🎉 POC Test Successful!
```

**In WhatsApp:** Bot replies with:
```
✅ POC Test Response from Adiyan!

📨 Your message was received and processed successfully.

Details captured:
• Contact: Your Name
• Phone: 919080089081
• Message: "Test message"
• Type: Individual
```

---

## Commands

```bash
# Start POC
./run.sh
# OR
node poc_test.js

# View logs
tail -f ~/.Adiyan/poc_test.log

# Reset WhatsApp session (rescan QR)
npm run reset
# Then: node poc_test.js
```

---

## What Gets Tested

✅ **Step 1**: Send WhatsApp message (you do this)  
✅ **Step 2**: Capture message in Node.js (automatic)  
✅ **Step 3**: Send response via WhatsApp (automatic)  

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| No QR code appears | Wait 20 seconds, check terminal |
| QR code won't scan | Make sure Linked Devices window is open |
| Message not received | Restart: `node poc_test.js` |
| Response didn't send | Check logs: `tail -f ~/.Adiyan/poc_test.log` |

---

**Status**: ✅ Ready to test!

Go ahead and run it! 🚀
