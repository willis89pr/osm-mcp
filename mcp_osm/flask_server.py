import json
import logging
import os
import queue
import sys
import threading
import time
import io
from contextlib import redirect_stdout

from unittest import mock

_log = logging.getLogger('werkzeug')
_log.setLevel(logging.WARNING)

# Redirect all Flask/Werkzeug logging to stderr
for handler in _log.handlers:
    handler.setStream(sys.stderr)

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_from_directory,
)


# Redirect all logging to stderr.
logging.basicConfig(stream=sys.stderr)

def log(msg):
    sys.stdout.flush()
    sys.stderr.write(f"{msg}\n")


class FlaskServer:
    def __init__(self, host="127.0.0.1", port=5000):
        # Capture and redirect Flask's initialization output to stderr
        with redirect_stdout(sys.stderr):
            self.app = Flask(
                __name__,
                template_folder=os.path.join(
                    os.path.dirname(os.path.dirname(__file__)), "templates"
                ),
                static_folder=os.path.join(
                    os.path.dirname(os.path.dirname(__file__)), "static"
                ),
            )
            # Disable default Flask logging output
            self.app.logger.disabled = True
            # Add handlers to redirect any remaining logs to stderr
            for handler in self.app.logger.handlers:
                handler.setStream(sys.stderr)
                
        self.host = host
        self.port = port
        self.server_thread = None
        self.current_view = {"center": [0, 0], "zoom": 2, "bounds": None}
        self.sse_clients = {}  # Changed from list to dict to store queues
        self.setup_routes()

    def setup_routes(self):
        @self.app.route("/")
        def index():
            return render_template("index.html")

        @self.app.route("/static/<path:path>")
        def send_static(path):
            return send_from_directory("static", path)

        @self.app.route("/api/sse")
        def sse():
            def event_stream(client_id):
                # Create a queue for this client
                client_queue = queue.Queue()
                self.sse_clients[client_id] = client_queue

                try:
                    # Initial connection message
                    yield 'data: {"type": "connected", "id": %d}\n\n' % client_id

                    while True:
                        try:
                            # Try to get a message from the queue with a timeout
                            message = client_queue.get(timeout=30)
                            yield f"data: {message}\n\n"
                        except queue.Empty:
                            # No message received in timeout period, send a ping
                            yield 'data: {"type": "ping"}\n\n'

                except GeneratorExit:
                    # Client disconnected
                    if client_id in self.sse_clients:
                        del self.sse_clients[client_id]
                    log(
                        f"Client {client_id} disconnected, {len(self.sse_clients)} clients remaining"
                    )

            # Generate a unique ID for this client
            client_id = int(time.time() * 1000) % 1000000
            return Response(event_stream(client_id), mimetype="text/event-stream")

        @self.app.route("/api/viewChanged", methods=["POST"])
        def view_changed():
            data = request.json
            if data:
                if "center" in data:
                    self.current_view["center"] = data["center"]
                if "zoom" in data:
                    self.current_view["zoom"] = data["zoom"]
                if "bounds" in data:
                    self.current_view["bounds"] = data["bounds"]
            return jsonify({"status": "success"})

    def start(self):
        """Start the Flask server in a separate thread"""

        def run_server():
            # Redirect stdout to stderr while running Flask
            with redirect_stdout(sys.stderr):
                self.app.run(
                    host=self.host, port=self.port, debug=False, use_reloader=False
                )

        self.server_thread = threading.Thread(target=run_server)
        self.server_thread.daemon = True  # Thread will exit when main thread exits
        self.server_thread.start()
        log(f"Flask server started at http://{self.host}:{self.port}")

    def stop(self):
        """Stop the Flask server"""
        # Flask doesn't provide a clean way to stop the server from outside
        # In a production environment, you would use a more robust server like gunicorn
        # For this example, we'll rely on the daemon thread to exit when the main thread exits
        log("Flask server stopping...")

    # Map control methods
    def send_map_command(self, command_type, data):
        """
        Send a command to all connected SSE clients

        Args:
            command_type (str): Type of command (SHOW_POLYGON, SHOW_MARKER, SET_VIEW)
            data (dict): Data for the command
        """
        command = {"type": command_type, "data": data}
        message = json.dumps(command)

        # Send the message to all connected clients
        clients_count = len(self.sse_clients)
        if clients_count == 0:
            log("No connected clients to send message to")
            return

        log(f"Sending {command_type} to {clients_count} clients")
        for client_id, client_queue in list(self.sse_clients.items()):
            try:
                client_queue.put(message)
            except Exception as e:
                log(f"Error sending to client {client_id}: {e}")

    def show_polygon(self, coordinates, options=None):
        """
        Display a polygon on the map

        Args:
            coordinates (list): List of [lat, lng] coordinates
            options (dict, optional): Styling options
        """
        data = {"coordinates": coordinates, "options": options or {}}
        self.send_map_command("SHOW_POLYGON", data)

    def show_marker(self, coordinates, text=None, options=None):
        """
        Display a marker on the map

        Args:
            coordinates (list): [lat, lng] coordinates
            text (str, optional): Popup text
            options (dict, optional): Styling options
        """
        data = {"coordinates": coordinates, "text": text, "options": options or {}}
        self.send_map_command("SHOW_MARKER", data)

    def set_view(self, bounds=None, center=None, zoom=None):
        """
        Set the map view

        Args:
            bounds (list, optional): [[south, west], [north, east]]
            center (list, optional): [lat, lng] center point
            zoom (int, optional): Zoom level
        """
        data = {}
        if bounds:
            data["bounds"] = bounds
        if center:
            data["center"] = center
        if zoom:
            data["zoom"] = zoom

        self.send_map_command("SET_VIEW", data)

    def get_current_view(self):
        """
        Get the current map view

        Returns:
            dict: Current view information
        """
        return self.current_view

    def set_title(self, title, options=None):
        """
        Set the map title displayed at the bottom right of the map

        Args:
            title (str): Title text to display
            options (dict, optional): Styling options like fontSize, color, etc.
        """
        data = {"title": title, "options": options or {}}
        self.send_map_command("SET_TITLE", data)


# For testing the Flask server directly
if __name__ == "__main__":
    server = FlaskServer()
    server.start()

    # Keep the main thread running
    try:
        log("Press Ctrl+C to stop the server")
        import time

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
        log("Server stopped")
