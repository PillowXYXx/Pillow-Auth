CRITICAL SECURITY UPDATE
========================

1. GET A NEW TOKEN:
   - Go to Discord Developer Portal -> Bot -> "Reset Token".
   - Copy the NEW token immediately.

2. CONFIGURE RENDER (DO NOT PASTE TOKEN IN CODE):
   - Go to your Render Dashboard -> "Environment" tab.
   - Update the "DISCORD_TOKEN" variable with your NEW token.
   - Save Changes. Render will auto-deploy.

3. UPLOAD CODE:
   - Upload this folder again. I have REMOVED the leaked token from the code.
   - This ensures Discord won't reset it again.

4. VERIFY:
   - Check Render Logs. It should say "Logged in as..."
