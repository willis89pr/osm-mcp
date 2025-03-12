import os
import threading
from flask import Flask, render_template, send_from_directory

class FlaskServer:
    def __init__(self, host='127.0.0.1', port=5000):
        self.app = Flask(__name__)
        self.host = host
        self.port = port
        self.server_thread = None
        self.setup_routes()
        
    def setup_routes(self):
        @self.app.route('/')
        def index():
            return render_template('index.html')
            
        @self.app.route('/static/<path:path>')
        def send_static(path):
            return send_from_directory('static', path)
    
    def start(self):
        """Start the Flask server in a separate thread"""
        def run_server():
            self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)
            
        self.server_thread = threading.Thread(target=run_server)
        self.server_thread.daemon = True  # Thread will exit when main thread exits
        self.server_thread.start()
        print(f"Flask server started at http://{self.host}:{self.port}")
        
    def stop(self):
        """Stop the Flask server"""
        # Flask doesn't provide a clean way to stop the server from outside
        # In a production environment, you would use a more robust server like gunicorn
        # For this example, we'll rely on the daemon thread to exit when the main thread exits
        print("Flask server stopping...")

# For testing the Flask server directly
if __name__ == "__main__":
    server = FlaskServer()
    server.start()
    
    # Keep the main thread running
    try:
        print("Press Ctrl+C to stop the server")
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
        print("Server stopped") 