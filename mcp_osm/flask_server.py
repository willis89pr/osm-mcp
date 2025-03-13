import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import io
import base64
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

# Create a logger for this module
logger = logging.getLogger(__name__)


class FlaskServer:
    def __init__(self, host="127.0.0.1", port=5000):
        self.host = host
        self.port = port
        self.app = Flask(__name__, 
                         template_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates"),
                         static_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), "static"))
        
        # Capture and redirect Flask's initialization output to stderr
        with io.StringIO() as buf, redirect_stdout(buf):
            self.setup_routes()
            output = buf.getvalue()
            if output:
                logger.info(output.strip())
        
        self.server_thread = None
        self.clients = {}  # Maps client_id to message queue
        self.client_counter = 0
        self.current_view = {
            "center": [0, 0],
            "zoom": 2,
            "bounds": [[-85, -180], [85, 180]]
        }
        self.sse_clients = {}  # Changed from list to dict to store queues
        self.latest_screenshot = None  # Store the latest screenshot
        
        # Add storage for geolocate requests and responses
        self.geolocate_requests = {}
        self.geolocate_responses = {}

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
                    logger.info(
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
            
        @self.app.route("/api/screenshot", methods=["POST"])
        def save_screenshot():
            data = request.json
            if data and "image" in data:
                # Store the base64 image data
                self.latest_screenshot = data["image"]
                return jsonify({"status": "success"})
            return jsonify({"status": "error", "message": "No image data provided"}), 400
            
        @self.app.route("/api/geolocateResponse", methods=["POST"])
        def geolocate_response():
            data = request.json
            if data and "requestId" in data and "results" in data:
                request_id = data["requestId"]
                results = data["results"]
                
                # Store the response
                self.geolocate_responses[request_id] = results
                logger.info(f"Received geolocate response for request {request_id} with {len(results)} results")
                
                return jsonify({"status": "success"})
            return jsonify({"status": "error", "message": "Invalid geolocate response data"}), 400

    def is_port_in_use(self, port):
        """Check if a port is already in use"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex((self.host, port)) == 0

    def start(self):
        """Start the Flask server in a separate thread"""
        # Try up to 10 ports, starting with self.port
        original_port = self.port
        max_attempts = 10
        
        for attempt in range(max_attempts):
            if self.is_port_in_use(self.port):
                logger.info(f"Port {self.port} is already in use, trying port {self.port + 1}")
                self.port += 1
                if attempt == max_attempts - 1:
                    logger.error(f"Failed to find an available port after {max_attempts} attempts")
                    # Reset port to original value
                    self.port = original_port
                    return False
            else:
                # Port is available, start the server
                def run_server():
                    # Redirect stdout to stderr while running Flask
                    with redirect_stdout(sys.stderr):
                        self.app.run(
                            host=self.host, port=self.port, debug=False, use_reloader=False
                        )
                
                self.server_thread = threading.Thread(target=run_server)
                self.server_thread.daemon = True  # Thread will exit when main thread exits
                self.server_thread.start()
                logger.info(f"Flask server started at http://{self.host}:{self.port}")
                return True
        
        return False

    def stop(self):
        """Stop the Flask server"""
        # Flask doesn't provide a clean way to stop the server from outside
        # In a production environment, you would use a more robust server like gunicorn
        # For this example, we'll rely on the daemon thread to exit when the main thread exits
        logger.info("Flask server stopping...")

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
            logger.info("No connected clients to send message to")
            return

        logger.info(f"Sending {command_type} to {clients_count} clients")
        for client_id, client_queue in list(self.sse_clients.items()):
            try:
                client_queue.put(message)
            except Exception as e:
                logger.error(f"Error sending to client {client_id}: {e}")

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

    def show_line(self, coordinates, options=None):
        """
        Display a line (polyline) on the map

        Args:
            coordinates (list): List of [lat, lng] coordinates
            options (dict, optional): Styling options
        """
        data = {"coordinates": coordinates, "options": options or {}}
        self.send_map_command("SHOW_LINE", data)

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
        
    def capture_screenshot(self):
        """
        Request a screenshot from the map and wait for it to be received
        
        Returns:
            str: Base64-encoded image data, or None if no screenshot is available
        """
        # Send command to capture screenshot
        self.send_map_command("CAPTURE_SCREENSHOT", {})
        
        # Wait for the screenshot to be received (with timeout)
        start_time = time.time()
        timeout = 5  # seconds
        
        while time.time() - start_time < timeout:
            if self.latest_screenshot:
                screenshot = self.latest_screenshot
                self.latest_screenshot = None  # Clear after retrieving
                return screenshot
            time.sleep(0.1)
        
        logger.warning("Screenshot capture timed out")
        return None
        
    def geolocate(self, query):
        """
        Send a geolocate request to the web client and wait for the response
        
        Args:
            query (str): The location name to search for
            
        Returns:
            list: Nominatim search results or None if the request times out
        """
        # Generate a unique request ID
        request_id = str(int(time.time() * 1000))
        
        # Send the geolocate command to the web client
        data = {"requestId": request_id, "query": query}
        self.send_map_command("GEOLOCATE", data)
        
        # Wait for the response (with timeout)
        start_time = time.time()
        timeout = 10  # seconds
        
        while time.time() - start_time < timeout:
            if request_id in self.geolocate_responses:
                results = self.geolocate_responses.pop(request_id)
                return results
            time.sleep(0.1)
        
        logger.warning(f"Geolocate request for '{query}' timed out")
        return None


# For testing the Flask server directly
if __name__ == "__main__":
    server = FlaskServer()
    server.start()

    # Keep the main thread running
    try:
        logger.info("Press Ctrl+C to stop the server")
        import time

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
        logger.info("Server stopped")
