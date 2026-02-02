import threading
import time
import os
import server
import bot

# 1. Run Flask Server in a separate thread
def run_server():
    # Ensure database is initialized
    server.init_db()
    # Get port from environment variable (Required for Render/Heroku)
    port = int(os.environ.get("PORT", 5000))
    # Run Flask (blocking)
    server.app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    print("---------------------------------------------------")
    print("   Pillow Player Cloud Launcher (All-in-One)       ")
    print("---------------------------------------------------")
    
    # Start Server Thread
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    
    # Start Keep-Alive Thread
    keep_alive_thread = threading.Thread(target=keep_alive_pinger, daemon=True)
    keep_alive_thread.start()
    
    print("[System] API Server started on port 5000.")
    print("[System] Waiting 3 seconds for server to initialize...")
    time.sleep(3)
    
    print("[System] Starting Discord Bot...")
    # Start Bot (Main Thread)
    try:
        bot.bot.run(bot.BOT_TOKEN)
    except Exception as e:
        print(f"CRITICAL ERROR: Bot failed to start: {e}")
        print("POSSIBLE FIXES:")
        print("1. CHECK YOUR TOKEN: Discord reset it because it was leaked. Generate a new one!")
        print("2. Set the NEW token in Render Environment Variables (Key: DISCORD_TOKEN).")
        print("3. Do NOT paste the token in the code.")
        print("[System] Keeping Web Server alive despite Bot failure...")
        
        # Keep the process alive so Render doesn't 502 (and we can see logs)
        while True:
            time.sleep(60)
