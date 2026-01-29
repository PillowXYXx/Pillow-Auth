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
    
    print("[System] API Server started on port 5000.")
    print("[System] Waiting 3 seconds for server to initialize...")
    time.sleep(3)
    
    print("[System] Starting Discord Bot...")
    # Start Bot (Main Thread)
    bot.bot.run(bot.BOT_TOKEN)
